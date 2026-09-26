#!/usr/bin/env bash
# Creates the benchmark's temporary host: one EC2 instance in the cell's VPC, apart from the cell, where the
# harness and every other memory system run, one system at a time. Niadra is measured where it runs, in
# its cell, through its public address and through its private address in the VPC.
#
#   deploy/temp-host/up.sh               print what would be created and what it costs; create nothing
#   deploy/temp-host/up.sh --confirm     create it, wait until it is ready, print the next commands
#
# What it creates, each tagged niadra:bench-temp-host=<id> (deploy/temp-host/down.sh deletes them all):
#   - a security group in the cell's VPC with no inbound rule (the host is reached through Systems
#     Manager only; it goes out to GitHub, the image registries, OpenRouter and Niadra's public address)
#   - an IAM role and instance profile: Systems Manager; read of the two secrets by name
#     (niadra/platform/openrouter, niadra/tenant/bootstrap); pull of the niadra/models image; write and
#     read of one S3 prefix of the cell's bucket (benchmarks/temp-host/<id>/)
#   - the instance (default m7i-flex.large: 2 vCPU, 8 GiB, free-tier eligible) in the cell's public subnet
#     with a public address, Ubuntu 24.04, a 40 GiB encrypted gp3 volume deleted with it, IMDSv2 only; it
#     terminates itself after BENCH_MAX_HOURS (default 24), whatever happens to this computer
#
# Needs the AWS session of the cell's account (`aws login --profile niadra`). See lib.sh for the settings.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

confirm=false
[ "${1:-}" = --confirm ] && confirm=true
[ "${1:-}" = -h ] || [ "${1:-}" = --help ] && { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

check_account
node="$(cell_node)"
[ -n "$node" ] || die "the stack $STACK has no InstanceId output"
vpc="$(node_field "$node" VpcId)"
subnet="$(node_field "$node" SubnetId)"
private_ip="$(node_field "$node" PrivateIpAddress)"
bucket="$(stack_output CellBucket)"
[ -n "$bucket" ] || die "the stack $STACK has no CellBucket output"
ami="$(aws_ ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text)"
# The embedder the cell runs right now (read on the cell's machine through Systems Manager, read only);
# the newest niadra/models image when that read fails.
models="$(ssm_run "$node" "kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n niadra get deploy models -o jsonpath='{.spec.template.spec.containers[0].image}'" 120 2>/dev/null | tr -d '[:space:]' || true)"
if [ -z "$models" ]; then
  tag="$(aws_ ecr describe-images --repository-name niadra/models \
    --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageTags[0]' --output text)"
  models="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/niadra/models:$tag"
fi
plan="$(aws_ freetier get-account-plan-state --region us-east-1 --query accountPlanType --output text 2>/dev/null || echo unknown)"
eligible="$(free_tier_eligible)"
id="$(date -u +%Y%m%d%H%M)-$(openssl rand -hex 2)"
prefix="$(prefix_of "$id")"
role="$(role_name "$id")"

cat <<EOF
Temporary benchmark host $id
  account $ACCOUNT, region $AWS_REGION, plan $plan
  instance $INSTANCE_TYPE (free-tier eligible: $eligible), Ubuntu 24.04 ($ami), ${VOLUME_GIB} GiB gp3
  VPC $vpc, subnet $subnet (the cell machine's), public address, security group with no inbound rule
  IAM role and instance profile $role
  S3 prefix s3://$bucket/$prefix/
  embedder image $models
  Niadra's private address in the VPC: $private_ip (the vpc path; the edge path is the public name)
  repository $REPO at $REF; terminates itself after $MAX_HOURS h
Cost (on-demand, paid from the account's credits on the free plan):
$(cost_estimate | sed 's/^/  /')
EOF
[ "$plan" = FREE ] && [ "$eligible" != True ] && die "$INSTANCE_TYPE is not free-tier eligible; the free plan cannot launch it"
if ! $confirm; then
  echo "Nothing created. Run again with --confirm to create it."
  exit 0
fi

mkdir -p "$STATE_DIR"
state="$(state_file "$id")"
record() { echo "$1=$2" >>"$state"; }
record ID "$id"
record BUCKET "$bucket"
record PREFIX "$prefix"
record ROLE "$role"
tags="ResourceType=%s,Tags=[{Key=$TAG_KEY,Value=$id},{Key=Name,Value=$NAME_PREFIX-$id}]"

echo "==> security group"
sg="$(aws_ ec2 create-security-group --group-name "$NAME_PREFIX-$id" --vpc-id "$vpc" \
  --description "Temporary benchmark host $id: no inbound rule, reached through Systems Manager" \
  --tag-specifications "$(printf "$tags" security-group)" --query GroupId --output text)"
record SECURITY_GROUP "$sg"

echo "==> IAM role $role"
aws_ iam create-role --role-name "$role" --tags "Key=$TAG_KEY,Value=$id" \
  --description "Temporary benchmark host $id" \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
aws_ iam attach-role-policy --role-name "$role" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws_ iam put-role-policy --role-name "$role" --policy-name benchmark-host --policy-document "$(cat <<JSON
{"Version": "2012-10-17", "Statement": [
  {"Sid": "SecretsByName", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
   "Resource": ["arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT:secret:$SECRET_OPENROUTER-*",
                "arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT:secret:$SECRET_BOOTSTRAP-*"]},
  {"Sid": "RegistryLogin", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
  {"Sid": "EmbedderImage", "Effect": "Allow",
   "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
   "Resource": "arn:aws:ecr:$AWS_REGION:$ACCOUNT:repository/niadra/models"},
  {"Sid": "ResultsPrefix", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
   "Resource": "arn:aws:s3:::$bucket/$prefix/*"},
  {"Sid": "ResultsList", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::$bucket",
   "Condition": {"StringLike": {"s3:prefix": ["$prefix/*"]}}}
]}
JSON
)"
aws_ iam create-instance-profile --instance-profile-name "$role" --tags "Key=$TAG_KEY,Value=$id" >/dev/null
aws_ iam add-role-to-instance-profile --instance-profile-name "$role" --role-name "$role"

echo "==> instance"
userdata="$(mktemp)"
trap 'rm -f "$userdata"' EXIT
sed -e "s#@HOST_ID@#$id#g" -e "s#@REGION@#$AWS_REGION#g" -e "s#@BUCKET@#$bucket#g" -e "s#@PREFIX@#$prefix#g" \
  -e "s#@MODELS_IMAGE@#$models#g" -e "s#@VPC_ADDRESS@#$private_ip#g" -e "s#@REPO@#$REPO#g" \
  -e "s#@REF@#$REF#g" -e "s#@MAX_MINUTES@#$((MAX_HOURS * 60))#g" "$HERE/user-data.sh" >"$userdata"
instance=""
error=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  # A new instance profile takes a few seconds to be usable by EC2.
  result="$(aws_ ec2 run-instances --image-id "$ami" --instance-type "$INSTANCE_TYPE" \
    --subnet-id "$subnet" --security-group-ids "$sg" --associate-public-ip-address \
    --iam-instance-profile "Name=$role" --instance-initiated-shutdown-behavior terminate \
    --metadata-options HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=2 \
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_GIB,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}" \
    --tag-specifications "$(printf "$tags" instance)" "$(printf "$tags" volume)" \
    --user-data "file://$userdata" --query 'Instances[0].InstanceId' --output text 2>&1 || true)"
  case "$result" in
    i-*) instance="$result"; break ;;
    *) error="$result"; sleep "$((POLL_S * 2))" ;;
  esac
done
[ -n "$instance" ] || say "$error"
[ -n "$instance" ] || die "the instance was not created; run down.sh $id to remove what was"
record INSTANCE "$instance"
aws_ ec2 wait instance-running --instance-ids "$instance"
echo "    $instance running; waiting for Systems Manager and the host's preparation (about 10 minutes)"
for _ in $(seq 1 90); do
  online="$(aws_ ssm describe-instance-information --filters "Key=InstanceIds,Values=$instance" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)"
  [ "$online" = Online ] && break
  sleep "$((POLL_S * 3))"
done
[ "$online" = Online ] || die "the host did not register with Systems Manager; run down.sh $id"
for _ in $(seq 1 120); do
  if out="$(ssm_run "$instance" 'test -f /opt/niadra-bench/ready && tail -n 3 /var/log/niadra-bench-prepare.log' 60 2>/dev/null)"; then
    echo "$out"
    break
  fi
  if ssm_run "$instance" 'test -f /opt/niadra-bench/failed' 60 >/dev/null 2>&1; then
    ssm_run "$instance" 'tail -n 40 /var/log/niadra-bench-prepare.log' 60 || true
    die "the host's preparation failed; run down.sh $id"
  fi
  sleep "$((POLL_S * 5))"
done
cat <<EOF
Ready: host $id ($instance). Next, from benchmarks/:
  deploy/temp-host/bench.sh campaign niadra mem0_oss,mem0_oss_rerank ai_memory ai_memory_llm hindsight hindsight_reflect memobase supermemory memos -- --dataset v2
  deploy/temp-host/bench.sh start graphiti --dataset v2 --limit 60 --repetitions 1   # after the campaign
  deploy/temp-host/bench.sh status            # repeat until the campaign log says finished
  deploy/temp-host/bench.sh collect           # the results, into benchmarks/results/
  deploy/temp-host/down.sh                    # delete everything, and check that nothing is left
EOF
