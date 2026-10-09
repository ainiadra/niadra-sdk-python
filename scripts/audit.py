# /// script
# requires-python = ">=3.11"
# dependencies = ["pip-audit==2.10.1"]
# ///
"""Known vulnerabilities in every package a lock pins, through pip-audit and the PyPI advisory database.

    uv run scripts/audit.py [uv.lock ...]

Every version in the lock is checked, the ones only a dev group or an extra installs included. Any finding
fails, whatever its severity: the advisory database does not grade them. A finding that cannot be fixed yet
goes into IGNORED with the day it stops being ignored, at most 30 days ahead; an entry past that day, or one
that matches nothing, fails too.
"""

import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import tomllib

PIP_AUDIT_FLAGS = ("--no-deps", "--disable-pip", "--progress-spinner", "off", "--format", "json")

_CREWAI = "crewai 1.15 (the crewai extra, tested in CI only) pins it, and no fixed release exists"
_WERKZEUG = (
    "semantic-kernel 1.45.0, the latest, caps openapi-core below 0.20, whose last release caps werkzeug "
    "below 3.1.2; low severity (safe_join and Windows device names), test-only through the semantic-kernel "
    "extra"
)
IGNORED: dict[str, tuple[date, str]] = {
    "GHSA-f4j7-r4q5-qw2c": (date(2026, 10, 30), f"chromadb: {_CREWAI}"),
    "GHSA-2wm9-hf6c-p5cr": (date(2026, 10, 30), f"chromadb: {_CREWAI}"),
    "GHSA-36p7-vc44-83pf": (date(2026, 10, 30), f"chromadb: {_CREWAI}"),
    "GHSA-xph7-9rjv-w5fr": (date(2026, 10, 30), f"chromadb: {_CREWAI}"),
    "GHSA-w8v5-vhqr-4h9v": (
        date(2026, 10, 30),
        "diskcache: dspy 3.4 (the dspy extra) needs it; no fixed release",
    ),
    "GHSA-8mgp-746c-j5xp": (
        date(2026, 10, 30),
        "nltk: llama-index-core and pipecat (their extras) need it; no fixed release",
    ),
    "GHSA-87hc-h4r5-73f7": (date(2026, 10, 30), _WERKZEUG),
    "GHSA-hgf8-39gv-g3f2": (date(2026, 10, 30), _WERKZEUG),
    "GHSA-29vq-49wr-vm6x": (date(2026, 10, 30), _WERKZEUG),
    "GHSA-g6x2-hccm-hh4m": (date(2026, 10, 30), _WERKZEUG),
}
"""Advisory id (or any of its aliases) -> (last day it is ignored, why it cannot be fixed yet)."""


def pin_rounds(lock: Path) -> list[list[str]]:
    """The lock's registry packages as `name==version` lines. A lock with forks pins some names twice, and
    pip-audit takes one version per name per file, so the second version of each name goes in a second
    round, and so on."""
    versions: dict[str, list[str]] = {}
    for package in tomllib.loads(lock.read_text())["package"]:
        if "registry" in package.get("source", {}):
            found = versions.setdefault(package["name"], [])
            if package["version"] not in found:
                found.append(package["version"])
    rounds = max((len(v) for v in versions.values()), default=0)
    return [[f"{name}=={v[i]}" for name, v in sorted(versions.items()) if i < len(v)] for i in range(rounds)]


def audit(pins: list[str]) -> list[dict[str, Any]]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as requirements:
        requirements.write("\n".join(pins) + "\n")
        requirements.flush()
        run = subprocess.run(  # noqa: S603 (a fixed command over a file this script wrote)
            [sys.executable, "-m", "pip_audit", *PIP_AUDIT_FLAGS, "--requirement", requirements.name],
            capture_output=True,
            text=True,
        )
    # pip-audit exits 1 when it finds something; anything else, or no report, is a failure to audit.
    if run.returncode not in (0, 1) or not run.stdout.strip():
        sys.exit(f"pip-audit failed ({run.returncode}):\n{run.stderr}")
    return [
        {"package": dep["name"], "version": dep["version"], **vuln}
        for dep in json.loads(run.stdout)["dependencies"]
        for vuln in dep.get("vulns", [])
    ]


def main(locks: list[Path]) -> int:
    today = date.today()
    found = [f for lock in locks for pins in pin_rounds(lock) for f in audit(pins)]
    # A version pinned in two locks, or an advisory the database lists twice, is reported once.
    findings = list({(f["package"], f["version"], f["id"]): f for f in found}.values())
    used: set[str] = set()
    failed = False
    for finding in findings:
        ids = [str(finding["id"]), *map(str, finding.get("aliases") or [])]
        entry = next((i for i in ids if i in IGNORED), None)
        fixed = ", ".join(map(str, finding.get("fix_versions") or [])) or "no fix yet"
        where = f"{finding['package']} {finding['version']} {finding['id']} (fixed in {fixed})"
        if entry is not None and IGNORED[entry][0] >= today:
            used.add(entry)
            print(f"ignored until {IGNORED[entry][0]}: {where}: {IGNORED[entry][1]}")
            continue
        failed = True
        print(f"vulnerable: {where}")
    for entry, (until, _) in IGNORED.items():
        if until < today:
            failed = True
            print(f"ignore entry expired on {until}: {entry}")
        elif entry not in used:
            failed = True
            print(f"ignore entry matches no finding, remove it: {entry}")
    if not failed:
        print(f"no known vulnerability outside IGNORED in {', '.join(map(str, locks))}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main([Path(p) for p in sys.argv[1:]] or [Path("uv.lock")]))
