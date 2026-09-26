# shellcheck shell=bash
# Shared by up.sh, down.sh and bench.sh, which run on the operator's computer with the AWS session of the
# account that runs the cell (`aws login --profile niadra`). Nothing here prints a secret.
#
# Environment: NIADRA_PROFILE (default niadra), NIADRA_REGION (default us-east-2), NIADRA_STACK (the
# cell's CloudFormation stack, default niadra), BENCH_ACCOUNT (the account the scripts refuse to leave,
# default 480916502925), BENCH_INSTANCE_TYPE (default m7i-flex.large), BENCH_VOLUME_GIB (default 40),
# BENCH_MAX_HOURS (the host terminates itself after this many hours, default 24), BENCH_REF (the branch
# or tag of this repository the host checks out, default main).

# shellcheck disable=SC2034 # the settings are read by the scripts that source this file
set -euo pipefail
export AWS_PROFILE="${NIADRA_PROFILE:-niadra}"
export AWS_REGION="${NIADRA_REGION:-us-east-2}"
export AWS_PAGER=""
STACK="${NIADRA_STACK:-niadra}"
ACCOUNT="${BENCH_ACCOUNT:-480916502925}"
INSTANCE_TYPE="${BENCH_INSTANCE_TYPE:-m7i-flex.large}"
VOLUME_GIB="${BENCH_VOLUME_GIB:-40}"
MAX_HOURS="${BENCH_MAX_HOURS:-24}"
REF="${BENCH_REF:-main}"
# Seconds between two polls of the AWS API (tests set 0).
POLL_S="${BENCH_POLL_S:-3}"
REPO="${BENCH_REPO:-https://github.com/ainiadra/niadra-sdk-python.git}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$HERE/.state"
# Every resource the scripts create carries this tag with the host's id; down.sh finds them by it.
TAG_KEY="niadra:bench-temp-host"
NAME_PREFIX="niadra-bench-temp"
# The secrets the host reads, by name only.
SECRET_OPENROUTER="niadra/platform/openrouter"
SECRET_BOOTSTRAP="niadra/tenant/bootstrap"

# Public on-demand prices in us-east-2 used when the Pricing API cannot be read (USD, checked 26/09/2026
# against the AWS price list: EC2 Linux on-demand per hour, gp3 per GiB-month, public IPv4 per hour).
fallback_hourly() {
  case "$1" in
    m7i-flex.large) echo 0.09576 ;;
    c7i-flex.large) echo 0.08479 ;;
    t3.small) echo 0.0208 ;;
    t3.micro) echo 0.0104 ;;
    *) echo "" ;;
  esac
}
GP3_GIB_MONTH=0.08
IPV4_HOURLY=0.005

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }

# Runs with a time limit: a hanging AWS call must not hang the script. macOS has no `timeout`.
limit() { local seconds="$1"; shift; perl -e 'alarm shift; exec @ARGV' "$seconds" "$@"; }

aws_() { limit 120 aws "$@"; }

check_account() {
  local id
  id="$(aws_ sts get-caller-identity --query Account --output text)" \
    || die "no AWS session: run \`aws login --profile $AWS_PROFILE\`"
  [ "$id" = "$ACCOUNT" ] || die "the session is account $id, not $ACCOUNT"
}

stack_output() {
  local value
  value="$(aws_ cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text)"
  [ "$value" = None ] && value=""
  echo "$value"
}

# The cell's machine: its instance id, VPC, subnet and private address (read only).
cell_node() { stack_output InstanceId; }
node_field() {
  aws_ ec2 describe-instances --instance-ids "$1" --query "Reservations[0].Instances[0].$2" --output text
}

# The hourly price of the instance type (on-demand, Linux, this region), from the Pricing API, or the
# list above when it cannot be read.
hourly_price() {
  local price location
  location="US East (Ohio)"
  [ "$AWS_REGION" = us-east-2 ] || location=""
  if [ -n "$location" ]; then
    price="$(limit 60 aws pricing get-products --region us-east-1 --service-code AmazonEC2 \
      --filters "Type=TERM_MATCH,Field=instanceType,Value=$INSTANCE_TYPE" \
      "Type=TERM_MATCH,Field=location,Value=$location" "Type=TERM_MATCH,Field=operatingSystem,Value=Linux" \
      "Type=TERM_MATCH,Field=tenancy,Value=Shared" "Type=TERM_MATCH,Field=preInstalledSw,Value=NA" \
      "Type=TERM_MATCH,Field=capacitystatus,Value=Used" --max-items 1 --output json 2>/dev/null |
      python3 -c '
import json, sys
data = json.load(sys.stdin)
item = json.loads(data["PriceList"][0])
for term in item["terms"]["OnDemand"].values():
    for dim in term["priceDimensions"].values():
        print(dim["pricePerUnit"]["USD"]); raise SystemExit
' 2>/dev/null || true)"
  fi
  [ -n "${price:-}" ] && { echo "$price"; return; }
  fallback_hourly "$INSTANCE_TYPE"
}

# Prints the estimate: per hour, per 8-hour run, per day; the host terminates itself at MAX_HOURS.
cost_estimate() {
  local instance total
  instance="$(hourly_price)"
  [ -n "$instance" ] || die "no price known for $INSTANCE_TYPE; set BENCH_INSTANCE_TYPE to a listed type"
  total="$(python3 -c "
instance, gib, gp3, ipv4, hours = $instance, $VOLUME_GIB, $GP3_GIB_MONTH, $IPV4_HOURLY, $MAX_HOURS
volume = gib * gp3 / 730
hourly = instance + volume + ipv4
print(f'instance {instance:.5f} + volume {volume:.5f} ({gib} GiB gp3) + public IPv4 {ipv4:.3f} = {hourly:.4f} USD/h')
print(f'8 h run: {hourly * 8:.2f} USD; one day: {hourly * 24:.2f} USD; the most it can cost before it terminates itself ({hours} h): {hourly * hours:.2f} USD')
")"
  printf '%s\n' "$total"
}

free_tier_eligible() {
  aws_ ec2 describe-instance-types --instance-types "$INSTANCE_TYPE" \
    --query 'InstanceTypes[0].FreeTierEligible' --output text 2>/dev/null || echo unknown
}

# The resources of a host, found by tag (so down.sh works without the state file).
tagged_instances() {
  aws_ ec2 describe-instances --filters "Name=tag:$TAG_KEY,Values=$1" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}
tagged_security_groups() {
  aws_ ec2 describe-security-groups --filters "Name=tag:$TAG_KEY,Values=$1" \
    --query 'SecurityGroups[].GroupId' --output text
}
tagged_volumes() {
  aws_ ec2 describe-volumes --filters "Name=tag:$TAG_KEY,Values=$1" --query 'Volumes[].VolumeId' --output text
}
role_name() { echo "$NAME_PREFIX-$1"; }
prefix_of() { echo "benchmarks/temp-host/$1"; }

# Runs a script on an instance through Systems Manager and prints its standard output (at most the
# 24,000 characters SSM keeps). Fails when the script fails. The script is written to a file on the other
# side and run with its standard input closed: a command inside it that reads standard input (the
# `kubectl run -i` of the old `bench down --drop-db`) can never read, and so never eat, the script.
ssm_run() {
  local instance="$1" script="$2" timeout="${3:-600}" command state
  command="$(aws_ ssm send-command --instance-ids "$instance" --document-name AWS-RunShellScript \
    --timeout-seconds "$timeout" \
    --parameters "$(python3 -c '
import json, sys
body = (
    "f=$(mktemp)\ncat >\"$f\" <<\"__BENCH_SCRIPT__\"\n" + sys.argv[1] + "\n__BENCH_SCRIPT__\n"
    "bash \"$f\" </dev/null; rc=$?; rm -f \"$f\"; exit $rc\n"
)
print(json.dumps({"commands": [body], "executionTimeout": [sys.argv[2]]}))' "$script" "$timeout")" \
    --query Command.CommandId --output text)"
  while true; do
    sleep "$POLL_S"
    state="$(aws_ ssm get-command-invocation --command-id "$command" --instance-id "$instance" \
      --query Status --output text 2>/dev/null || echo Pending)"
    case "$state" in Pending | InProgress | Delayed) ;; *) break ;; esac
  done
  aws_ ssm get-command-invocation --command-id "$command" --instance-id "$instance" \
    --query StandardOutputContent --output text
  if [ "$state" != Success ]; then
    aws_ ssm get-command-invocation --command-id "$command" --instance-id "$instance" \
      --query StandardErrorContent --output text | tail -n 40 >&2
    return 1
  fi
}

state_file() { echo "$STATE_DIR/$1.env"; }
latest_id() {
  local newest
  newest="$(ls -1t "$STATE_DIR"/*.env 2>/dev/null | head -1)"
  [ -n "$newest" ] && basename "$newest" .env
}
