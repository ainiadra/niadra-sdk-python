"""`bench stack`: one results folder from several runs of the same systems, their repetitions one after the
other, so a system measured in separate runs (one repetition each, with the production cell left to drain
between them) has the repetitions a single run would have had.

The runs must be the same measurement: the same dataset and configuration hashes, the same cases, the same
systems, the same references setting, the same harness commit and the same Niadra server version (a stack
never mixes two deployments). Repetition k of the stack is the k-th repetition in the order the folders are
given; its rows and its `rep-k.json` are the source's, renumbered, and `rep-k.json` names the source run
and repetition (`stacked_from`).
Nothing is measured again or recomputed but the summary across the repetitions (`runner.aggregate`), and
`stacked_from` lists every source with the repetitions it gave. The folder is a run like any other: `bench
combine` takes it as its first folder when it has the references.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: What must be equal in every stacked run, as (section, field).
SAME: tuple[tuple[str, str], ...] = (
    ("dataset", "hash"),
    ("dataset", "cases"),
    ("config", "hash"),
    ("config", "references"),
    ("config", "limit"),
    ("config", "quick"),
    ("versions", "harness_commit"),
    ("versions", "niadra_server"),
)


def _systems(directory: Path) -> set[str]:
    systems: set[str] = set()
    for path in directory.glob("cases-rep*.jsonl"):
        for line in path.read_text().splitlines():
            if line.strip():
                systems.add(json.loads(line)["system"])
    return systems


def _check(directories: list[Path], summaries: list[dict[str, Any]]) -> None:
    if len(directories) < 2:
        raise ValueError("stack needs at least two runs")
    first = summaries[0]
    systems = _systems(directories[0])
    for directory, other in zip(directories[1:], summaries[1:], strict=True):
        for section, field in SAME:
            if other[section].get(field) != first[section].get(field):
                raise ValueError(
                    f"{other['run_id']} has another {section} {field} than {first['run_id']}: "
                    "only runs of the same measurement stack"
                )
        if _systems(directory) != systems:
            raise ValueError(f"{other['run_id']} measured other systems than {first['run_id']}")


def stack(directories: list[Path], output: Path) -> Path:
    from niadra_bench.runner import aggregate

    summaries = [json.loads((d / "summary.json").read_text()) for d in directories]
    _check(directories, summaries)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    target = output / f"{summaries[0]['date']}-{run_id[-6:]}"
    target.mkdir(parents=True, exist_ok=False)
    reps: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for directory, summary in zip(directories, summaries, strict=True):
        own = int(summary["config"]["repetitions"])
        for n in range(1, own + 1):
            k = len(reps) + 1
            rep = json.loads((directory / f"rep-{n}.json").read_text())
            rep["repetition"] = k
            rep["stacked_from"] = {"run_id": summary["run_id"], "repetition": n}
            with (target / f"cases-rep{k}.jsonl").open("w") as handle:
                for line in (directory / f"cases-rep{n}.jsonl").read_text().splitlines():
                    if line.strip():
                        row = json.loads(line)
                        row["repetition"] = k
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            (target / f"rep-{k}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
            reps.append(rep)
        sources.append(
            {
                "run_id": summary["run_id"],
                "repetitions": own,
                "started_at": summary["started_at"],
                "finished_at": summary["finished_at"],
            }
        )

    combined = dict(summaries[0])
    combined["run_id"] = run_id
    combined["started_at"] = min(s["started_at"] for s in summaries)
    combined["finished_at"] = max(s["finished_at"] for s in summaries)
    combined["config"] = {**summaries[0]["config"], "repetitions": len(reps)}
    combined["dataset"] = {
        **summaries[0]["dataset"],
        "valid": _across([r.get("validity", {}).get("valid") for r in reps]),
        "excluded": [r.get("validity", {}).get("excluded", []) for r in reps],
    }
    combined["stacked_from"] = sources
    combined["metrics"] = aggregate(reps)
    (target / "summary.json").write_text(json.dumps(combined, indent=2, ensure_ascii=False) + "\n")
    return target


def _across(values: list[Any]) -> dict[str, Any]:
    from niadra_bench import stats

    return stats.across(values, 0)
