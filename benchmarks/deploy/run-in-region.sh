#!/usr/bin/env bash
# Runs the benchmark inside the region, on the k3s machine, as root (through Systems Manager, like
# niadra-infra's scripts). Needs git, docker, kubectl and k3s on the machine, which the cell's machine has.
#
#   run-in-region.sh up                 build both images from this repository, import them into k3s,
#                                       create Mem0's databases and start Mem0, the proxy and the meter
#   run-in-region.sh start [bench args] start the run Job (default: every system, every metric)
#   run-in-region.sh status             the Job's state and the last lines of its log
#   run-in-region.sh collect            copy the newest results folder to the cell's bucket
#                                       (s3://<bucket>/benchmarks/<folder>/) and print its summary.json
#   run-in-region.sh down [--drop-db]   remove every benchmark object (and Mem0's databases)
#
# Environment: BENCH_REPO (default the public SDK repository), BENCH_REF (default main).
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml PATH="$PATH:/usr/local/bin"
REPO="${BENCH_REPO:-https://github.com/ainiadra/niadra-sdk-python.git}"
REF="${BENCH_REF:-main}"
WORK=/var/lib/niadra-bench
SRC="$WORK/src"
NS=niadra
LABEL=app.kubernetes.io/part-of=niadra-benchmarks
MEM0_IMAGE=niadra-bench/mem0:v2.2.0
DEFAULT_ARGS='["run", "--systems", "niadra,mem0_oss,mem0_oss_rerank"]'

checkout() {
  if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch -q origin "$REF" && git -C "$SRC" checkout -q --detach FETCH_HEAD
  else
    git clone -q --branch "$REF" --depth 1 "$REPO" "$SRC"
  fi
  git -C "$SRC" rev-parse HEAD
}

harness_image() { echo "niadra-bench/harness:$(git -C "$SRC" rev-parse --short=12 HEAD)"; }

render() {  # file: replaces the placeholders and prints the manifest. The server version is matched with
  # its quotes: bare, it would also rewrite the name of the variable BENCH_NIADRA_SERVER_VERSION.
  sed -e "s#HARNESS_IMAGE#$(harness_image)#g" -e "s#MEM0_IMAGE#$MEM0_IMAGE#g" \
    -e "s#BENCH_ARGS#${BENCH_ARGS_JSON:-$DEFAULT_ARGS}#g" \
    -e "s#\"NIADRA_SERVER_VERSION\"#\"${NIADRA_SERVER_VERSION:-unknown}\"#g" "$SRC/benchmarks/deploy/k8s/$1"
}

up() {
  mkdir -p "$WORK/results" && chown 10001 "$WORK/results"
  local commit
  commit="$(checkout)"
  echo "==> images from $commit"
  docker build -q -f "$SRC/benchmarks/deploy/Dockerfile" --build-arg HARNESS_COMMIT="$commit" \
    -t "$(harness_image)" "$SRC/benchmarks" >/dev/null
  docker build -q -t "$MEM0_IMAGE" "$SRC/benchmarks/deploy/mem0" >/dev/null
  docker save "$(harness_image)" "$MEM0_IMAGE" | k3s ctr -n k8s.io images import - >/dev/null
  echo "==> secrets"
  if ! kubectl -n "$NS" get secret bench-mem0 >/dev/null 2>&1; then
    kubectl -n "$NS" create secret generic bench-mem0 \
      --from-literal=MEM0_DB_PASSWORD="$(openssl rand -hex 24)" \
      --from-literal=ADMIN_API_KEY="$(openssl rand -hex 24)" \
      --from-literal=JWT_SECRET="$(openssl rand -hex 32)" >/dev/null
    kubectl -n "$NS" label secret bench-mem0 "$LABEL" >/dev/null
  fi
  render 00-secrets.yaml | kubectl apply -f - >/dev/null
  kubectl -n "$NS" wait --for=condition=Ready externalsecret/bench-openrouter --timeout=120s >/dev/null
  echo "==> Mem0 databases"
  kubectl -n "$NS" delete job bench-mem0-db-init --ignore-not-found >/dev/null
  render 10-db-init.yaml | kubectl apply -f - >/dev/null
  kubectl -n "$NS" wait --for=condition=complete job/bench-mem0-db-init --timeout=300s >/dev/null
  echo "==> Mem0 server, embedding proxy and meter"
  render 20-services.yaml | kubectl apply -f - >/dev/null
  for d in bench-embed bench-meter bench-mem0; do kubectl -n "$NS" rollout status "deploy/$d" --timeout=600s; done
}

start() {
  checkout >/dev/null
  if [ "$#" -gt 0 ]; then
    BENCH_ARGS_JSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@")"
    export BENCH_ARGS_JSON
  fi
  NIADRA_SERVER_VERSION="$(kubectl -n "$NS" get deploy read -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://')"
  export NIADRA_SERVER_VERSION
  kubectl -n "$NS" delete job bench-run --ignore-not-found >/dev/null
  render 30-run.yaml | kubectl apply -f - >/dev/null
  echo "started bench-run against niadra server $NIADRA_SERVER_VERSION with args ${BENCH_ARGS_JSON:-$DEFAULT_ARGS}"
}

status() {
  kubectl -n "$NS" get job bench-run -o wide || true
  kubectl -n "$NS" logs job/bench-run --tail=40 || true
}

collect() {
  local latest
  latest="$(ls -1dt "$WORK"/results/*/ 2>/dev/null | head -1)"
  [ -n "$latest" ] || { echo "no results yet"; exit 1; }
  echo "==> $latest"
  ls -la "$latest"
  local bucket target
  bucket="$(kubectl -n "$NS" get configmap niadra-cell -o jsonpath='{.data.NIADRA_S3_BUCKET}')"
  target="${BENCH_S3_URI:-s3://$bucket/benchmarks}/$(basename "$latest")/"
  aws s3 cp --recursive --only-show-errors "$latest" "$target"
  echo "copied to $target"
  echo "==> summary.json"
  cat "$latest/summary.json"
}

down() {
  kubectl -n "$NS" delete job,deploy,service,externalsecret,secret -l "$LABEL" --ignore-not-found
  kubectl -n "$NS" delete secret bench-mem0 --ignore-not-found
  if [ "${1:-}" = "--drop-db" ]; then
    kubectl -n "$NS" run bench-drop-db --rm -i --restart=Never --image=postgres:17-alpine \
      --overrides='{"spec":{"containers":[{"name":"bench-drop-db","image":"postgres:17-alpine","stdin":true,"envFrom":[{"secretRef":{"name":"niadra-db-init"}}],"env":[{"name":"PGDATABASE","value":"postgres"},{"name":"PGHOST","valueFrom":{"configMapKeyRef":{"name":"niadra-cell","key":"NIADRA_DB_DIRECT_HOST"}}},{"name":"PGSSLMODE","value":"require"}],"command":["psql","-v","ON_ERROR_STOP=1","-c","DROP DATABASE IF EXISTS mem0_bench WITH (FORCE)","-c","DROP DATABASE IF EXISTS mem0_bench_app WITH (FORCE)","-c","DROP ROLE IF EXISTS mem0_bench"]}]}}'
  fi
  docker image rm "$MEM0_IMAGE" >/dev/null 2>&1 || true
}

case "${1:-}" in
  up) up ;;
  start) shift; start "$@" ;;
  status) status ;;
  collect) collect ;;
  down) shift; down "$@" ;;
  *) sed -n '2,16p' "$0"; exit 1 ;;
esac
