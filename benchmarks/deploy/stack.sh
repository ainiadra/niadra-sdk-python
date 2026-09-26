#!/usr/bin/env bash
# Puts the benchmark's compose files together for one environment and some systems, then runs
# docker compose on them:
#
#   deploy/stack.sh <local|temp-host> <system,...> <docker compose arguments>
#
#   deploy/stack.sh local ai_memory,mem0_oss up -d          start what those systems need, with fakes
#   deploy/stack.sh local ai_memory run --rm harness run --dry-run --systems ai_memory --quick
#   deploy/stack.sh local ai_memory down -v                  remove it all, volumes included
#
# The pieces: deploy/compose/base.yaml (the embedding proxy and the harness), deploy/<env>/env.yaml (the
# embedder and the model provider of that environment) and, for each system, the deploy/systems/<dir>/
# compose.yaml whose first line names it (`# systems: <key> ...`). Niadra runs in its cell and needs no
# file here. The files are put together with `include`, so each keeps its own relative paths.
#
# Environment: BENCH_PROJECT (the compose project name, default niadra-bench), BENCH_HARNESS_IMAGE
# (default niadra-bench/harness:local, built by `deploy/stack.sh <env> <systems> build-harness`).
set -euo pipefail
DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DEPLOY")"

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ "$#" -ge 3 ] || usage
env="$1" systems="$2"
shift 2
case "$env" in local | temp-host) ;; *) usage ;; esac

# The directory of each system's compose file, from the `# systems:` line on its first line.
dirs=()
IFS=',' read -r -a wanted <<<"$systems"
for key in "${wanted[@]}"; do
  [ -n "$key" ] || continue
  [ "$key" = niadra ] && continue
  found=""
  for file in "$DEPLOY"/systems/*/compose.yaml; do
    if head -1 "$file" | tr ' ' '\n' | tail -n +3 | grep -qx "$key"; then
      found="$(basename "$(dirname "$file")")"
      break
    fi
  done
  [ -n "$found" ] || { echo "no deploy/systems/*/compose.yaml names the system $key" >&2; exit 2; }
  case " ${dirs[*]-} " in *" $found "*) ;; *) dirs+=("$found") ;; esac
done

project="${BENCH_PROJECT:-niadra-bench}"
mkdir -p "$DEPLOY/.stack"
top="$DEPLOY/.stack/$project.yaml"
{
  echo "# Written by deploy/stack.sh for: $env $systems"
  echo "include:"
  echo "  - path: [../compose/base.yaml, ../$env/env.yaml]"
  for dir in "${dirs[@]-}"; do
    [ -n "$dir" ] && echo "  - ../systems/$dir/compose.yaml"
  done
} >"$top"

export BENCH_HARNESS_IMAGE="${BENCH_HARNESS_IMAGE:-niadra-bench/harness:local}"
if [ "${1:-}" = build-harness ]; then
  docker build -q -f "$DEPLOY/Dockerfile" \
    --build-arg HARNESS_COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)" \
    -t "$BENCH_HARNESS_IMAGE" "$ROOT" >/dev/null
  echo "built $BENCH_HARNESS_IMAGE"
  exit 0
fi
exec docker compose -p "$project" -f "$top" "$@"
