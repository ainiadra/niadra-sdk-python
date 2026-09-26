#!/usr/bin/env bash
# Removes what the benchmark left on the cell from the time it ran there (until 25/09/2026: the harness,
# Mem0 and the proxies as pods of the cell, Mem0's databases on the cell's RDS instance). The benchmark no
# longer runs on the cell (deploy/temp-host), so after this nothing of it is left on the cell's machine or
# database.
#
#   deploy/cell/cleanup.sh              list what is left; change nothing
#   deploy/cell/cleanup.sh --confirm    remove it, then list again (exits 1 if anything is still there)
#
# Removed: the Kubernetes objects labelled app.kubernetes.io/part-of=niadra-benchmarks and the bench-mem0
# secret in the niadra namespace; the databases mem0_bench and mem0_bench_app and the role mem0_bench on
# the RDS instance (as the master user, from a pod with the niadra-db-init secret); the benchmark's images in
# the node's containerd. The script is a file run with its standard input closed (lib.sh, ssm_run), the
# pod only waits, and the SQL goes in as `psql -c` arguments through `kubectl exec` with no standard input,
# so no command inside it can read the rest of the script as its own input.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../temp-host" && pwd)/lib.sh"

confirm=false
[ "${1:-}" = --confirm ] && confirm=true
check_account
node="$(cell_node)"
[ -n "$node" ] || die "the stack $STACK has no InstanceId output"

read -r -d '' SCRIPT <<'NODE' || true
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml PATH="$PATH:/usr/local/bin"
NS=niadra
LABEL=app.kubernetes.io/part-of=niadra-benchmarks
CONFIRM=@CONFIRM@
list() {
  echo "--- Kubernetes objects"
  kubectl -n "$NS" get job,deploy,service,externalsecret,secret,pod -l "$LABEL" -o name 2>/dev/null || true
  kubectl -n "$NS" get secret bench-mem0 -o name 2>/dev/null || true
  echo "--- node images"
  k3s ctr -n k8s.io images ls -q 2>/dev/null | grep -E '^(docker\.io/)?niadra-bench/' || true
}
pod="bench-cleanup-$RANDOM"
sql() {  # runs one statement per argument, as the master user; no standard input anywhere
  local args=()
  for statement in "$@"; do args+=(-c "$statement"); done
  kubectl -n "$NS" exec "$pod" -- psql -X -At -v ON_ERROR_STOP=1 -d postgres "${args[@]}"
}
start_pod() {
  kubectl -n "$NS" run "$pod" --quiet --restart=Never --image=postgres:17-alpine \
    --labels="niadra.com/db-client=true" \
    --overrides='{"spec":{"activeDeadlineSeconds":600,"containers":[{"name":"psql","image":"postgres:17-alpine","command":["sleep","600"],"envFrom":[{"secretRef":{"name":"niadra-db-init"}}],"env":[{"name":"PGHOST","valueFrom":{"configMapKeyRef":{"name":"niadra-cell","key":"NIADRA_DB_DIRECT_HOST"}}},{"name":"PGSSLMODE","value":"require"}]}]}}' >/dev/null
  kubectl -n "$NS" wait pod "$pod" --for=condition=Ready --timeout=120s >/dev/null
}
start_pod
trap 'kubectl -n "$NS" delete pod "$pod" --wait=false >/dev/null 2>&1 || true' EXIT
databases() {
  echo "--- RDS"
  sql "SELECT 'database ' || datname FROM pg_database WHERE datname IN ('mem0_bench', 'mem0_bench_app') UNION ALL SELECT 'role ' || rolname FROM pg_roles WHERE rolname = 'mem0_bench'"
}
list
databases
if [ "$CONFIRM" = true ]; then
  echo "=== removing"
  kubectl -n "$NS" delete job,deploy,service,externalsecret,secret -l "$LABEL" --ignore-not-found
  kubectl -n "$NS" delete secret bench-mem0 --ignore-not-found
  sql "DROP DATABASE IF EXISTS mem0_bench WITH (FORCE)" "DROP DATABASE IF EXISTS mem0_bench_app WITH (FORCE)" \
    "DROP ROLE IF EXISTS mem0_bench"
  for image in $(k3s ctr -n k8s.io images ls -q 2>/dev/null | grep -E '^(docker\.io/)?niadra-bench/' || true); do
    k3s ctr -n k8s.io images rm "$image" >/dev/null
  done
  for image in $(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep '^niadra-bench/' || true); do
    docker image rm "$image" >/dev/null || true
  done
  echo "=== after"
  left="$(list | grep -v '^---' || true)$(databases | grep -v '^---' || true)"
  list
  databases
  [ -z "$left" ] || { echo "still there"; exit 1; }
  echo "nothing of the benchmark is left on the cell"
fi
NODE
ssm_run "$node" "${SCRIPT//@CONFIRM@/$confirm}" 900
$confirm || echo "Nothing changed. Run again with --confirm to remove what is listed."
