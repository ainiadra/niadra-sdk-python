"""deploy/temp-host/up.sh and down.sh against a stand-in `aws` command that records every call and answers
like the account would, so the scripts are exercised end to end without touching AWS."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "deploy" / "temp-host"

STUB = r"""#!PYTHON
import json, os, sys
args = sys.argv[1:]
log = os.environ["STUB_LOG"]
state_path = os.environ["STUB_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {"created": {}}
with open(log, "a") as handle:
    handle.write(json.dumps(args) + "\n")
def out(text):
    print(text)
    json.dump(state, open(state_path, "w"))
    sys.exit(0)
def fail(text):
    print(text, file=sys.stderr)
    json.dump(state, open(state_path, "w"))
    sys.exit(254)
query = args[args.index("--query") + 1] if "--query" in args else ""
cmd = " ".join(a for a in args[:2])
if cmd == "sts get-caller-identity": out("480916502925")
if cmd == "cloudformation describe-stacks":
    out("i-0cell" if "InstanceId" in query else "niadra-cell-480916502925")
if cmd == "ec2 describe-instances":
    if "--instance-ids" in args:
        field = query.rsplit(".", 1)[-1]
        out({"VpcId": "vpc-1", "SubnetId": "subnet-public", "PrivateIpAddress": "10.40.1.10"}[field])
    alive = [i for i in state["created"].get("instances", []) if i not in state.get("terminated", [])]
    out(" ".join(alive) if alive else "")
if cmd == "ssm get-parameter": out("ami-ubuntu2404")
if cmd == "ssm send-command":
    params = json.loads(args[args.index("--parameters") + 1])
    if os.environ.get("STUB_EXECUTE"):
        # Runs the command as the instance would (sh, the body written to a file), with the stand-ins.
        import subprocess
        done = subprocess.run(["sh", "-c", params["commands"][0]], capture_output=True, text=True, timeout=60)
        status = "Success" if done.returncode == 0 else "Failed"
        state["last"] = {"status": status, "out": done.stdout, "err": done.stderr}
    else:
        image = "123.dkr.ecr.us-east-2.amazonaws.com/niadra/models:v42"
        state["last"] = {"status": "Success", "out": image, "err": ""}
    out("cmd-1")
if cmd == "ssm get-command-invocation":
    last = state.get("last", {})
    if "Status" in query: out(last.get("status", "Success"))
    if "StandardOutputContent" in query: out(last.get("out", ""))
    if "StandardErrorContent" in query: out(last.get("err", ""))
    out("")
if cmd == "ssm describe-instance-information": out("Online")
if cmd == "freetier get-account-plan-state": out("FREE")
if cmd == "ec2 describe-instance-types": out("True")
if cmd == "pricing get-products": fail("no pricing in tests")
if cmd == "ec2 create-security-group":
    state["created"]["sg"] = "sg-1"; out("sg-1")
if cmd == "iam create-role": state["created"]["role"] = args[args.index("--role-name") + 1]; out("{}")
if cmd in ("iam attach-role-policy", "iam put-role-policy", "iam add-role-to-instance-profile"): out("")
if cmd == "iam create-instance-profile": state["created"]["profile"] = True; out("{}")
if cmd == "ec2 run-instances":
    state["created"].setdefault("instances", []).append("i-0host"); out("i-0host")
if cmd == "ec2 wait": out("")
if cmd == "ec2 describe-volumes": out("")
if cmd == "ec2 describe-security-groups":
    out("sg-1" if state["created"].get("sg") else "")
if cmd == "ec2 terminate-instances":
    state.setdefault("terminated", []).append("i-0host"); out("")
if cmd == "ec2 delete-security-group": state["created"].pop("sg", None); out("")
if cmd == "iam get-instance-profile":
    out("arn:profile") if state["created"].get("profile") else fail("NoSuchEntity")
if cmd == "iam remove-role-from-instance-profile": out("")
if cmd == "iam delete-instance-profile": state["created"].pop("profile", None); out("")
if cmd == "iam get-role":
    out("arn:role") if state["created"].get("role") else fail("NoSuchEntity")
if cmd == "iam list-role-policies": out("benchmark-host")
if cmd == "iam list-attached-role-policies": out("arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
if cmd in ("iam delete-role-policy", "iam detach-role-policy"): out("")
if cmd == "iam delete-role": state["created"].pop("role", None); out("")
if cmd == "s3 ls": out("")
if cmd == "s3 rm": out("")
fail("unexpected: " + " ".join(args))
"""


@pytest.fixture
def aws(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "aws"
    stub.write_text(STUB.replace("#!PYTHON", f"#!{sys.executable}"))
    stub.chmod(0o755)
    deploy = tmp_path / "deploy"
    work = deploy / "temp-host"
    shutil.copytree(HERE, work, ignore=shutil.ignore_patterns(".state"))
    shutil.copytree(HERE.parent / "cell", deploy / "cell")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_STATE": str(tmp_path / "state.json"),
        "BENCH_POLL_S": "0",
    }

    def run(script: str, *args: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(deploy / script if "/" in script else work / script), *args],
            env={**env, **(extra or {})},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def calls() -> list[list[str]]:
        path = tmp_path / "calls.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    run.calls = calls  # type: ignore[attr-defined]
    run.work = work  # type: ignore[attr-defined]
    return run


def test_up_without_confirm_prints_the_plan_and_the_cost_and_creates_nothing(aws) -> None:
    result = aws("up.sh")
    assert result.returncode == 0, result.stderr
    assert "m7i-flex.large (free-tier eligible: True)" in result.stdout
    assert "= 0.1051 USD/h" in result.stdout  # 0.09576 + 40 GiB of gp3 (0.00438) + public IPv4 (0.005)
    assert "Nothing created" in result.stdout
    created = [
        c for c in aws.calls() if any(w.startswith(("create-", "run-", "put-", "attach-")) for w in c[:2])
    ]
    assert created == []


def test_up_creates_a_scoped_host_and_down_deletes_it_and_checks(aws) -> None:
    up = aws("up.sh", "--confirm")
    assert up.returncode == 0, up.stderr
    calls = aws.calls()
    run = next(c for c in calls if c[:2] == ["ec2", "run-instances"])
    assert run[run.index("--instance-type") + 1] == "m7i-flex.large"
    assert run[run.index("--subnet-id") + 1] == "subnet-public"
    assert "HttpTokens=required" in run[run.index("--metadata-options") + 1]
    assert "Encrypted=true,DeleteOnTermination=true" in run[run.index("--block-device-mappings") + 1]
    assert run[run.index("--instance-initiated-shutdown-behavior") + 1] == "terminate"
    sg = next(c for c in calls if c[:2] == ["ec2", "create-security-group"])
    assert not [c for c in calls if c[:2] == ["ec2", "authorize-security-group-ingress"]]
    assert sg[sg.index("--vpc-id") + 1] == "vpc-1"
    policy = json.loads(next(c for c in calls if c[:2] == ["iam", "put-role-policy"])[-1])
    resources = json.dumps(policy)
    assert "niadra/platform/openrouter-*" in resources and "niadra/tenant/bootstrap-*" in resources
    assert "repository/niadra/models" in resources and "benchmarks/temp-host/" in resources
    assert '"s3:DeleteObject"' not in resources and "secretsmanager:*" not in resources
    userdata = Path(run[run.index("--user-data") + 1].removeprefix("file://"))
    assert not userdata.exists()  # removed after launch; it never held a secret
    state = next((aws.work / ".state").glob("*.env")).read_text()
    assert "INSTANCE=i-0host" in state and "SECURITY_GROUP=sg-1" in state

    down = aws("down.sh")
    assert down.returncode == 0, down.stdout + down.stderr
    assert "nothing left" in down.stdout
    rm = next(c for c in aws.calls() if c[:2] == ["s3", "rm"])
    target = rm[-1]
    assert target.startswith("s3://niadra-cell-480916502925/benchmarks/temp-host/") and "*" not in target
    assert not list((aws.work / ".state").glob("*.env"))


def test_user_data_holds_no_secret_and_ends_the_host_by_itself() -> None:
    text = (HERE / "user-data.sh").read_text()
    assert "shutdown -h +@MAX_MINUTES@" in text
    assert "api_key" not in text and "OPENROUTER" not in text.upper().replace("OPENROUTER_API_KEY", "")


NODE_TOOLS = r"""#!/bin/bash
# kubectl, k3s and docker as the cell's machine has them, for the cleanup script: every call is logged;
# the database answers what `psql` was asked.
echo "$(basename "$0") $*" >>"$NODE_LOG"
if [ ! -t 0 ] && [ "$(basename "$0")" = kubectl ] && [ "${*: -1}" != "--wait=false" ]; then
  # A command that reads its standard input here would eat the rest of the script.
  if read -r -t 0.2 line; then echo "READ STDIN: $line" >>"$NODE_LOG"; fi
fi
case "$*" in
  *"psql"*"SELECT 'database '"*) [ -f "$NODE_LOG.dropped" ] || echo "database mem0_bench" ;;
  *"psql"*"DROP DATABASE"*) touch "$NODE_LOG.dropped" ;;
esac
exit 0
"""


def test_the_cell_cleanup_drops_mem0s_databases_through_ssm_without_eating_the_script(aws, tmp_path) -> None:
    tools = tmp_path / "node-bin"
    tools.mkdir()
    for name in ("kubectl", "k3s", "docker"):
        (tools / name).write_text(NODE_TOOLS)
        (tools / name).chmod(0o755)
    node_log = tmp_path / "node.log"
    extra = {"STUB_EXECUTE": "1", "NODE_LOG": str(node_log), "PATH": f"{tools}:{os.environ['PATH']}"}
    listing = aws("cell/cleanup.sh", extra={**extra, "PATH": f"{tmp_path / 'bin'}:{extra['PATH']}"})
    assert listing.returncode == 0, listing.stderr
    assert "database mem0_bench" in listing.stdout and "Nothing changed" in listing.stdout
    assert "DROP" not in node_log.read_text()
    removed = aws(
        "cell/cleanup.sh", "--confirm", extra={**extra, "PATH": f"{tmp_path / 'bin'}:{extra['PATH']}"}
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    log = node_log.read_text()
    assert (
        "DROP DATABASE IF EXISTS mem0_bench WITH (FORCE)" in log and "DROP ROLE IF EXISTS mem0_bench" in log
    )
    assert "READ STDIN" not in log
    assert "nothing of the benchmark is left on the cell" in removed.stdout
