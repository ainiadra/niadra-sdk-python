#!/usr/bin/env bash
# Measures Niadra's `host` path from the cell's own machine, the way every other system is measured from
# the harness's host: a pod on the cell's node runs `bench run` with only the `host` path, so each request
# goes straight to the deployable that serves it (read, ingest) over plain HTTP inside the cluster, with no
# public DNS, no TLS and no ingress (net.py). The numbers are Niadra's lines of metrics 1, 8 and 9 on that
# path; the other paths are measured from the temporary host as before (deploy/temp-host).
#
#   deploy/cell/host-lines.sh [--ref <branch or commit>] [-- <bench run args>]
#
# Default run: `bench run --systems niadra --metrics latency,history,ingest --dataset v2 --limit 20
# --dry-run --no-references`, the first 20 cases seeded under a new tag (the latency lines read 20
# conversations), at the production caps (`BENCH_ENVIRONMENT=region`). `--dry-run` only spares the agent
# (no metric here asks one); every request to Niadra is real. The pod reads the tenant's bootstrap keys
# with the node's own role (secrets under the cell's name), never through this computer or the command's
# output. It is labelled app.kubernetes.io/part-of=niadra-benchmarks, deleted at the end, and
# deploy/cell/cleanup.sh finds any left over. The results stay on the node under
# /var/tmp/niadra-bench-host/<run>; the summary is printed.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../temp-host" && pwd)/lib.sh"

ref="$REF"
if [ "${1:-}" = --ref ]; then
  ref="${2:?--ref needs a branch or commit}"
  shift 2
fi
[ "${1:-}" = -- ] && shift
args=("$@")
# shellcheck disable=SC2054 # the commas are inside one argument, the metrics' list
[ ${#args[@]} -gt 0 ] || args=(--systems niadra --metrics latency,history,ingest --dataset v2 --limit 20
  --dry-run --no-references)
check_account
node="$(cell_node)"
[ -n "$node" ] || die "the stack $STACK has no InstanceId output"
quoted="$(printf ' %q' "${args[@]}")"

read -r -d '' SCRIPT <<'NODE' || true
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml PATH="$PATH:/usr/local/bin"
NS=niadra
run="$(date -u +%Y%m%dT%H%M%SZ)"
pod="bench-host-$RANDOM"
out="/var/tmp/niadra-bench-host/$run"
mkdir -p "$out"
kubectl -n "$NS" run "$pod" --quiet --restart=Never --image=python:3.13-slim \
  --labels=app.kubernetes.io/part-of=niadra-benchmarks \
  --overrides='{"spec":{"activeDeadlineSeconds":7200,"containers":[{"name":"bench","image":"python:3.13-slim","command":["sleep","7200"]}]}}' >/dev/null
trap 'kubectl -n "$NS" delete pod "$pod" --wait=false >/dev/null 2>&1 || true' EXIT
kubectl -n "$NS" wait pod "$pod" --for=condition=Ready --timeout=300s >/dev/null
# The harness at @REF@, installed from the public repository; the bootstrap keys read with the node's role.
kubectl -n "$NS" exec "$pod" -- sh -c '
set -e
cd /tmp
python -c "import urllib.request; urllib.request.urlretrieve(\"https://codeload.github.com/ainiadra/niadra-sdk-python/tar.gz/@REF@\", \"src.tgz\")"
mkdir src && tar -xzf src.tgz -C src --strip-components 1
pip install --quiet --root-user-action=ignore ./src/benchmarks boto3 >/dev/null
python -c "import boto3; open(\"/tmp/bootstrap.json\", \"w\").write(boto3.client(\"secretsmanager\", region_name=\"@REGION@\").get_secret_value(SecretId=\"niadra/tenant/bootstrap\")[\"SecretString\"])"
chmod 0400 /tmp/bootstrap.json
'
kubectl -n "$NS" exec "$pod" -- env \
  NIADRA_BOOTSTRAP=/tmp/bootstrap.json NIADRA_CONTROL_URL=https://control.api.niadra.com \
  "NIADRA_HOST_ADDRESS=http://{service}.$NS.svc.cluster.local:8000" NIADRA_PATHS=host \
  BENCH_ENVIRONMENT=region BENCH_ROOT=/tmp/src/benchmarks \
  sh -c 'cd /tmp/src/benchmarks && bench run --output /tmp/results @ARGS@'
kubectl -n "$NS" cp "$pod:/tmp/results" "$out" >/dev/null
echo "results on the node: $out"
find "$out" -name summary.md -exec cat {} \; 2>/dev/null | head -80
NODE
SCRIPT="${SCRIPT//@REF@/$ref}"
SCRIPT="${SCRIPT//@REGION@/$AWS_REGION}"
SCRIPT="${SCRIPT//@ARGS@/$quoted}"
ssm_run "$node" "$SCRIPT" 7200
