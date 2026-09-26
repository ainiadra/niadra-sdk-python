#!/usr/bin/env bash
# Runs ON the temporary host (as root, through Systems Manager; bench.sh sends these). The host was
# prepared by its user data (up.sh): Docker, the AWS CLI and this repository at /opt/niadra-bench/src.
#
#   host.sh prepare                         read the secrets (by name), pull the embedder, build the harness
#   host.sh start <systems> [bench args]    start those systems and one `bench run` in the background
#   host.sh ab [bench ab args]              one `bench ab` against the cell (Niadra only), in the background
#   host.sh campaign <runs> [-- bench args] one run after the other, each alone on the host: <runs> is a
#                                           space-separated list of comma-separated system lists; each run
#                                           starts its systems, runs, uploads its results, removes them
#   host.sh status                          the containers, and the last lines of the run's log
#   host.sh collect                         copy every results folder to s3://<bucket>/<prefix>/results/
#   host.sh stop                            stop the run (it closes its targets) and remove every system
#
# Nothing here prints a secret: they are written to root-only files and passed to docker compose through
# the environment of this process only.
set -euo pipefail
BASE=/opt/niadra-bench
# shellcheck source=/dev/null
. "$BASE/host.env"
DEPLOY="$BASE/src/benchmarks/deploy"
SECRETS="$BASE/secrets"
RESULTS="$BASE/results"
LOGS="$BASE/logs"
RUN_NAME=niadra-bench-run
export BENCH_PROJECT=niadra-bench
HARNESS_TAG="$(git -C "$BASE/src" rev-parse --short=12 HEAD)"
export BENCH_HARNESS_IMAGE="niadra-bench/harness:$HARNESS_TAG"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
mkdir -p "$LOGS"

stack() { "$DEPLOY/stack.sh" temp-host "$@"; }

# The environment docker compose reads: the host's settings, and the secrets from their files.
environment() {
  set -a
  # shellcheck source=/dev/null
  . "$SECRETS/env"
  set +a
  export BENCH_ENVIRONMENT=region NIADRA_VPC_ADDRESS BENCH_MODELS_IMAGE="$MODELS_IMAGE"
  export BENCH_LLM_UPSTREAM=https://openrouter.ai/api/v1
  export BENCH_RESULTS_DIR="$RESULTS" BENCH_SECRETS_DIR="$SECRETS/harness"
  export NIADRA_BOOTSTRAP=/secrets/bootstrap.json NIADRA_CONTROL_URL=https://control.api.niadra.com
}

prepare() {
  install -d -m 0700 "$SECRETS" "$SECRETS/harness"
  install -d -o 10001 -m 0755 "$RESULTS"
  umask 077
  local openrouter
  openrouter="$(aws secretsmanager get-secret-value --secret-id niadra/platform/openrouter \
    --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
  {
    echo "BENCH_LLM_API_KEY=$openrouter"
    # The local systems' own tokens and passwords: random, for this host only.
    for name in AI_MEMORY_AUTH_TOKEN MEMOBASE_ACCESS_TOKEN MEMOBASE_DB_PASSWORD MEMOBASE_REDIS_PASSWORD \
      MEM0_DB_PASSWORD MEM0_ADMIN_API_KEY MEM0_JWT_SECRET GRAPHITI_NEO4J_PASSWORD MEMOS_NEO4J_PASSWORD; do
      echo "$name=$(openssl rand -hex 24)"
    done
  } >"$SECRETS/env"
  unset openrouter
  aws secretsmanager get-secret-value --secret-id niadra/tenant/bootstrap --query SecretString \
    --output text >"$SECRETS/harness/bootstrap.json"
  chown 10001 "$SECRETS/harness" "$SECRETS/harness/bootstrap.json"
  chmod 0400 "$SECRETS/harness/bootstrap.json"
  umask 022
  # The embedder the cell runs, from the account's registry.
  aws ecr get-login-password | docker login --username AWS --password-stdin "${MODELS_IMAGE%%/*}" >/dev/null
  docker pull -q "$MODELS_IMAGE" >/dev/null
  docker build -q -f "$DEPLOY/Dockerfile" --build-arg HARNESS_COMMIT="$(git -C "$BASE/src" rev-parse HEAD)" \
    -t "$BENCH_HARNESS_IMAGE" "$DEPLOY/.." >/dev/null
  echo "prepared: harness $BENCH_HARNESS_IMAGE, embedder $MODELS_IMAGE"
}

# One run: the systems' containers, then `bench run` with the given arguments, in the foreground.
run_once() {
  local systems="$1"
  shift
  environment
  stack "$systems" up -d --build --quiet-pull >/dev/null
  docker rm -f "$RUN_NAME" >/dev/null 2>&1 || true
  stack "$systems" run --name "$RUN_NAME" harness run --systems "$systems" "$@"
}

start() {
  local systems="${1:?usage: host.sh start <systems> [bench args]}"
  shift
  environment
  stack "$systems" up -d --build --quiet-pull >/dev/null
  docker rm -f "$RUN_NAME" >/dev/null 2>&1 || true
  stack "$systems" run -d --name "$RUN_NAME" harness run --systems "$systems" "$@" >/dev/null
  echo "started $RUN_NAME: bench run --systems $systems $*"
}

ab() {
  environment
  stack niadra up -d --quiet-pull >/dev/null
  docker rm -f "$RUN_NAME" >/dev/null 2>&1 || true
  stack niadra run -d --name "$RUN_NAME" harness ab "$@" >/dev/null
  echo "started $RUN_NAME: bench ab $*"
}

campaign() {
  # A transient systemd service, so the campaign outlives the Systems Manager command that starts it.
  systemctl reset-failed niadra-bench-campaign >/dev/null 2>&1 || true
  systemd-run --unit niadra-bench-campaign --collect --quiet \
    --setenv=PATH="/snap/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    --setenv=BENCH_NIADRA_SERVER_VERSION="${BENCH_NIADRA_SERVER_VERSION:-}" \
    /bin/bash "$0" run-campaign "$@"
  echo "campaign started: $* (log: $LOGS/campaign.log)"
}

run_campaign() {
  local runs=() args=()
  while [ "$#" -gt 0 ]; do
    if [ "$1" = -- ]; then shift; args=("$@"); break; fi
    runs+=("$1")
    shift
  done
  [ "${#runs[@]}" -gt 0 ] || { echo "usage: host.sh campaign <systems> [<systems> ...] [-- bench args]"; exit 2; }
  exec >>"$LOGS/campaign.log" 2>&1 </dev/null
  for systems in "${runs[@]}"; do
    echo "==> $(date -u +%FT%TZ) $systems"
    run_once "$systems" "${args[@]}" || echo "==> $systems failed"
    collect || true
    stack "$systems" down -v --remove-orphans >/dev/null 2>&1 || true
    docker rm -f "$RUN_NAME" >/dev/null 2>&1 || true
  done
  echo "==> $(date -u +%FT%TZ) campaign finished"
}

status() {
  docker ps --format '{{.Names}}\t{{.Status}}' | sort
  if [ -f "$LOGS/campaign.log" ]; then echo "--- campaign"; grep '^==>' "$LOGS/campaign.log" | tail -20; fi
  systemctl is-active --quiet niadra-bench-campaign && echo "--- campaign running"
  if docker inspect "$RUN_NAME" >/dev/null 2>&1; then
    echo "--- $RUN_NAME: $(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' "$RUN_NAME")"
    docker logs --tail 25 "$RUN_NAME" 2>&1
  fi
  free -m | head -2
}

collect() {
  local dir
  for dir in "$RESULTS"/*/; do
    [ -f "$dir/summary.json" ] || continue
    aws s3 cp --recursive --only-show-errors "$dir" "s3://$BUCKET/$PREFIX/results/$(basename "$dir")/"
    echo "collected $(basename "$dir")"
  done
  for dir in "$RESULTS"/ab/*/; do
    [ -f "$dir/ab.json" ] || continue
    aws s3 cp --recursive --only-show-errors "$dir" "s3://$BUCKET/$PREFIX/results/ab/$(basename "$dir")/"
    echo "collected ab/$(basename "$dir")"
  done
}

stop() {
  environment
  systemctl stop niadra-bench-campaign >/dev/null 2>&1 || true
  docker stop -t 90 "$RUN_NAME" >/dev/null 2>&1 || true
  local all
  all="$(docker run --rm "$BENCH_HARNESS_IMAGE" systems --compose | awk '{print $1}' | paste -sd, -)"
  stack "niadra,mem0_oss,$all" down -v --remove-orphans >/dev/null 2>&1 || true
  echo "stopped"
}

case "${1:-}" in
  prepare) prepare ;;
  start) shift; start "$@" ;;
  ab) shift; ab "$@" ;;
  campaign) shift; campaign "$@" ;;
  run-campaign) shift; run_campaign "$@" ;;
  status) status ;;
  collect) collect ;;
  stop) stop ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
