#!/usr/bin/env bash
# Deletes everything up.sh created for a temporary benchmark host, then checks that nothing is left:
#
#   deploy/temp-host/down.sh [<id>] [--discard-results]
#
# <id> defaults to the newest host this computer created (deploy/temp-host/.state/). Resources are found by
# their tag (niadra:bench-temp-host=<id>) and by name, so it also works without the state file. It refuses
# while the host's S3 prefix holds a results folder that is not in benchmarks/results/ yet (bench.sh
# collect copies them), unless --discard-results. Deleted, in order: the instance (and its volume), the
# security group, the instance profile, the role and its policies, and the S3 prefix
# benchmarks/temp-host/<id>/ (that prefix only). Exits 1 if anything is still there at the end.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

id="" discard=false
for arg in "$@"; do
  case "$arg" in
    --discard-results) discard=true ;;
    -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) id="$arg" ;;
  esac
done
[ -n "$id" ] || id="$(latest_id || true)"
[ -n "$id" ] || die "no host id given and none in $STATE_DIR"
case "$id" in *[!0-9a-f-]* | "") die "not a host id: $id" ;; esac
check_account
bucket="$(stack_output CellBucket)"
[ -n "$bucket" ] || die "the stack $STACK has no CellBucket output"
prefix="$(prefix_of "$id")"
role="$(role_name "$id")"
RESULTS="$(cd "$HERE/../.." && pwd)/results"

echo "==> results in s3://$bucket/$prefix/results/"
missing=""
for run in $(aws_ s3 ls "s3://$bucket/$prefix/results/" 2>/dev/null | awk '/PRE/ {print $2}' | tr -d / | grep -vx ab || true); do
  [ -f "$RESULTS/$run/summary.json" ] || missing="$missing $run"
done
for run in $(aws_ s3 ls "s3://$bucket/$prefix/results/ab/" 2>/dev/null | awk '/PRE/ {print $2}' | tr -d / || true); do
  [ -f "$RESULTS/ab/$run/ab.json" ] || missing="$missing ab/$run"
done
if [ -n "$missing" ] && ! $discard; then
  die "not collected yet:$missing. Run bench.sh collect $id first, or down.sh $id --discard-results"
fi

echo "==> instance"
instances="$(tagged_instances "$id")"
if [ -n "$instances" ] && [ "$instances" != None ]; then
  # shellcheck disable=SC2086
  aws_ ec2 terminate-instances --instance-ids $instances >/dev/null
  # shellcheck disable=SC2086
  limit 900 aws ec2 wait instance-terminated --instance-ids $instances
fi

echo "==> volumes"
for volume in $(tagged_volumes "$id"); do
  [ "$volume" = None ] && continue
  aws_ ec2 delete-volume --volume-id "$volume" || true
done

echo "==> security group"
for group in $(tagged_security_groups "$id"); do
  [ "$group" = None ] && continue
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    # The network interface of a terminated instance takes a moment to go.
    aws_ ec2 delete-security-group --group-id "$group" 2>/dev/null && break
    sleep "$((POLL_S * 3))"
  done
done

echo "==> instance profile and role $role"
if aws_ iam get-instance-profile --instance-profile-name "$role" >/dev/null 2>&1; then
  aws_ iam remove-role-from-instance-profile --instance-profile-name "$role" --role-name "$role" 2>/dev/null || true
  aws_ iam delete-instance-profile --instance-profile-name "$role"
fi
if aws_ iam get-role --role-name "$role" >/dev/null 2>&1; then
  for policy in $(aws_ iam list-role-policies --role-name "$role" --query 'PolicyNames' --output text); do
    [ "$policy" = None ] || aws_ iam delete-role-policy --role-name "$role" --policy-name "$policy"
  done
  for arn in $(aws_ iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text); do
    [ "$arn" = None ] || aws_ iam detach-role-policy --role-name "$role" --policy-arn "$arn"
  done
  aws_ iam delete-role --role-name "$role"
fi

echo "==> s3://$bucket/$prefix/"
# The host's own prefix, spelled out: never a wildcard, never a shorter prefix.
case "$prefix" in benchmarks/temp-host/"$id") ;; *) die "refusing to remove $prefix" ;; esac
aws_ s3 rm --recursive --only-show-errors "s3://$bucket/$prefix/"

echo "==> check"
left=0
check() {
  if [ -n "$2" ] && [ "$2" != None ]; then
    echo "  still there: $1 $2"
    left=1
  else
    echo "  gone: $1"
  fi
}
check instance "$(tagged_instances "$id")"
check volume "$(tagged_volumes "$id")"
check "security group" "$(tagged_security_groups "$id")"
check "instance profile" "$(aws_ iam get-instance-profile --instance-profile-name "$role" --query InstanceProfile.Arn --output text 2>/dev/null || true)"
check role "$(aws_ iam get-role --role-name "$role" --query Role.Arn --output text 2>/dev/null || true)"
check "S3 prefix" "$(aws_ s3 ls --recursive "s3://$bucket/$prefix/" 2>/dev/null | head -1)"
if [ "$left" = 0 ]; then
  rm -f "$(state_file "$id")"
  echo "host $id: nothing left"
fi
exit "$left"
