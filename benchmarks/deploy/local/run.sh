#!/usr/bin/env bash
# The local pipeline, the way the temporary host runs it: each comma-separated system list is one dry run
# on Docker, alone (its containers started, measured, removed with their volumes), with fakes for the models
# and niadra-mock for Niadra; then `bench combine` puts the runs together. It proves the plumbing only: no
# number from it is ever published.
#
#   deploy/local/run.sh [<systems> ...] [-- <bench run args>]
#
#   deploy/local/run.sh niadra,mem0_oss,mem0_oss_rerank ai_memory graphiti -- --dataset v2 --quick --limit 14
#
# Without systems it runs every one: Niadra and Mem0 together, then each added system. The default bench
# arguments are `--dataset v2 --quick --limit 14 --repetitions 1`. Results: deploy/local/results/.
set -euo pipefail
DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(dirname "$DEPLOY")"
export BENCH_PROJECT="${BENCH_PROJECT:-niadra-bench-dry}"
limit() { local seconds="$1"; shift; perl -e 'alarm shift; exec @ARGV' "$seconds" "$@"; }

runs=() args=()
while [ "$#" -gt 0 ]; do
  if [ "$1" = -- ]; then shift; args=("$@"); break; fi
  runs+=("$1")
  shift
done
[ "${#args[@]}" -gt 0 ] || args=(--dataset v2 --quick --limit 14 --repetitions 1)
if [ "${#runs[@]}" -eq 0 ]; then
  runs=("niadra,mem0_oss,mem0_oss_rerank")
  while read -r key _; do runs+=("$key"); done < <(cd "$ROOT" && uv run --quiet bench systems --compose)
fi

mkdir -p "$DEPLOY/local/results" "$DEPLOY/local/secrets"
"$DEPLOY/stack.sh" local niadra build-harness
folders=()
for systems in "${runs[@]}"; do
  echo "==> $systems"
  limit 1800 "$DEPLOY/stack.sh" local "$systems" up -d --build --quiet-pull >/dev/null
  before="$(ls -1 "$DEPLOY/local/results" | sort)"
  if limit 3600 "$DEPLOY/stack.sh" local "$systems" run --rm harness run --dry-run --systems "$systems" \
    --output /app/results "${args[@]}"; then
    new="$(comm -13 <(echo "$before") <(ls -1 "$DEPLOY/local/results" | sort) | tail -1)"
    [ -n "$new" ] && folders+=("$DEPLOY/local/results/$new")
  else
    echo "==> $systems failed"
  fi
  limit 600 "$DEPLOY/stack.sh" local "$systems" down -v --remove-orphans >/dev/null 2>&1 || true
done
if [ "${#folders[@]}" -gt 1 ]; then
  (cd "$ROOT" && uv run --quiet bench combine "${folders[@]}" --output "$DEPLOY/local/results")
fi
