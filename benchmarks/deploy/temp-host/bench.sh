#!/usr/bin/env bash
# Drives the temporary benchmark host from this computer, through Systems Manager (no port is open):
#
#   bench.sh campaign <systems> [<systems> ...] [-- <bench run args>]
#                              each comma-separated list is one run, alone on the host, one after the
#                              other, in the background; each run's results go to the host's S3 prefix
#   bench.sh start <systems> [<bench run args>]   one run, in the background
#   bench.sh ab [<bench ab args>]                 one A/B of Niadra's settings against the cell
#   bench.sh status                               containers, the campaign's progress, the run's last lines
#   bench.sh collect                              copy every results folder to benchmarks/results/
#   bench.sh stop                                 stop the run (its billing key is revoked) and the systems
#
# The host is the newest one up.sh created from this computer, or BENCH_HOST=<id>. Every run gets the
# Niadra server version the cell runs at that moment (read on the cell's machine, read only).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

command="${1:-}"
[ -n "$command" ] || { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
shift
id="${BENCH_HOST:-$(latest_id || true)}"
[ -n "$id" ] || die "no host: run up.sh --confirm first (or set BENCH_HOST)"
state="$(state_file "$id")"
[ -f "$state" ] || die "no state for host $id in $STATE_DIR"
# shellcheck source=/dev/null
. "$state"
check_account
HOST="/opt/niadra-bench/src/benchmarks/deploy/temp-host/host.sh"

quoted() { printf ' %q' "$@"; }

server_version() {
  ssm_run "$(cell_node)" "kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n niadra get deploy read -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'" 120 2>/dev/null | tr -d '[:space:]' || true
}

case "$command" in
  campaign | start | ab)
    version="$(server_version)"
    echo "Niadra server ${version:-unknown}"
    ssm_run "$INSTANCE" "BENCH_NIADRA_SERVER_VERSION=$(printf %q "$version") $HOST $command$(quoted "$@")" 600
    ;;
  status) ssm_run "$INSTANCE" "$HOST status" 120 ;;
  stop) ssm_run "$INSTANCE" "$HOST stop" 600 ;;
  collect)
    ssm_run "$INSTANCE" "$HOST collect" 600
    results="$(cd "$HERE/../.." && pwd)/results"
    for run in $(aws_ s3 ls "s3://$BUCKET/$PREFIX/results/" | awk '/PRE/ {print $2}' | tr -d / | grep -vx ab || true); do
      limit 600 aws s3 cp --recursive --only-show-errors "s3://$BUCKET/$PREFIX/results/$run/" "$results/$run/"
      echo "results/$run"
    done
    for run in $(aws_ s3 ls "s3://$BUCKET/$PREFIX/results/ab/" 2>/dev/null | awk '/PRE/ {print $2}' | tr -d /); do
      limit 600 aws s3 cp --recursive --only-show-errors "s3://$BUCKET/$PREFIX/results/ab/$run/" "$results/ab/$run/"
      echo "results/ab/$run"
    done
    ;;
  *) die "unknown command $command" ;;
esac
