#!/bin/bash
# The temporary benchmark host's first boot (up.sh fills the @...@ values; no secret is in here: the host
# reads its secrets by name with its own role, in host.sh prepare).
set -euxo pipefail
# Whatever happens to the operator's computer, the host ends itself (the instance terminates on shutdown).
shutdown -h +@MAX_MINUTES@ "the benchmark host's time is up" || true
fail() { touch /opt/niadra-bench/failed; }
trap fail ERR
mkdir -p /opt/niadra-bench
cat >/opt/niadra-bench/host.env <<ENV
HOST_ID=@HOST_ID@
REGION=@REGION@
BUCKET=@BUCKET@
PREFIX=@PREFIX@
MODELS_IMAGE=@MODELS_IMAGE@
NIADRA_VPC_ADDRESS=@VPC_ADDRESS@
ENV
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends docker.io docker-buildx docker-compose-v2 git jq python3 openssl ca-certificates
snap install aws-cli --classic
systemctl enable --now docker
git clone -q --depth 1 --branch "@REF@" "@REPO@" /opt/niadra-bench/src
PATH="/snap/bin:$PATH" bash /opt/niadra-bench/src/benchmarks/deploy/temp-host/host.sh prepare \
  >/var/log/niadra-bench-prepare.log 2>&1
touch /opt/niadra-bench/ready
