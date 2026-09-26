"""`bench combine`: one results folder from several runs of the same dataset and configuration, each run
with its own systems (the temporary host measures one system at a time, so each has the host alone).

Per repetition, the accuracy pass is summarized again over all the runs' rows with one validity rule:
the two references (no memory, full history) of the first run decide which cases count, for every
system. The other metrics' lines are put together as they are; a line two runs share (the embedder's
`encode` line, Niadra's public prices) is kept once, from the first run that has it. The combined
`summary.json` has the schema the site imports, with the list of runs it came from.

The runs may differ in repetitions and in cases (`--limit`), since the slow and the costly systems run
fewer: the first run needs the most repetitions and every case, a run without references
(`--no-references`) can only come after it, and each source in `combined_from` says how many repetitions
and cases it measured, so a system measured on fewer is shown as such.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from niadra_bench.dataset.model import Case
from niadra_bench.metrics import accuracy, backing

#: The fields that make two lines of a metric the same line.
KEYS: dict[str, tuple[str, ...]] = {
    "latency": ("system", "path", "rate"),
    "freshness": ("system",),
    "resilience": ("system", "fault"),
    "history": ("system", "path", "operation", "rate"),
    "ingest": ("system", "path", "operation", "rate"),
    "cost": ("system", "variant"),
}
REFERENCES = frozenset({"no_memory", "full_history"})


def _rows(directory: Path, n: int) -> list[accuracy.CaseRow]:
    path = directory / f"cases-rep{n}.jsonl"
    if not path.exists():
        return []
    return [accuracy.CaseRow(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def _check_same(summaries: list[dict[str, Any]]) -> None:
    first = summaries[0]
    if first["config"].get("references", "on") == "off":
        raise ValueError(f"{first['run_id']} has no references: the first run's decide validity")
    for other in summaries[1:]:
        for section, field in (("dataset", "hash"), ("config", "hash")):
            if other[section][field] != first[section][field]:
                raise ValueError(
                    f"{other['run_id']} has another {section} {field} than {first['run_id']}: "
                    "only runs of the same dataset and configuration combine"
                )
        if other["config"]["repetitions"] > first["config"]["repetitions"]:
            raise ValueError(
                f"{other['run_id']} has more repetitions than {first['run_id']}: the first run's "
                "references cover only its own repetitions"
            )
        if other["dataset"]["cases"] > first["dataset"]["cases"]:
            raise ValueError(f"{other['run_id']} has more cases than {first['run_id']}")


def combine(directories: list[Path], output: Path, cases: dict[str, Case]) -> Path:
    from niadra_bench.runner import aggregate

    summaries = [json.loads((d / "summary.json").read_text()) for d in directories]
    _check_same(summaries)
    repetitions = int(summaries[0]["config"]["repetitions"])
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    target = output / f"{summaries[0]['date']}-{run_id[-6:]}"
    target.mkdir(parents=True, exist_ok=True)
    reps: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for n in range(1, repetitions + 1):
        rows: list[accuracy.CaseRow] = []
        seen_systems: set[tuple[str, str | None]] = set()
        rep: dict[str, Any] = {"repetition": n, "seed": {}, "settle": {}, "combined": True}
        for index, directory in enumerate(directories):
            if not (directory / f"rep-{n}.json").exists():
                # A run with fewer repetitions (a costly or slow system) counts in its own only.
                continue
            run_rows = _rows(directory, n)
            systems = {(r.system, r.scenario) for r in run_rows}
            keep = {s for s in systems if s[0] not in REFERENCES or index == 0} - seen_systems
            rows += [r for r in run_rows if (r.system, r.scenario) in keep]
            seen_systems |= keep
            raw = json.loads((directory / f"rep-{n}.json").read_text())
            for part in ("seed", "settle"):
                for label, value in (raw.get(part) or {}).items():
                    rep[part].setdefault(label, value)
            for metric, fields in KEYS.items():
                have = {tuple(line.get(f) for f in fields) for line in rep.get(metric, [])}
                for line in raw.get(metric) or []:
                    key = tuple(line.get(f) for f in fields)
                    if key not in have:
                        rep.setdefault(metric, []).append(line)
                        have.add(key)
            if n == 1:
                sources.append(
                    {
                        "run_id": summaries[index]["run_id"],
                        "systems": sorted({s for s, _ in systems} - REFERENCES),
                        "repetitions": summaries[index]["config"]["repetitions"],
                        "cases": summaries[index]["dataset"]["cases"],
                        "started_at": summaries[index]["started_at"],
                        "finished_at": summaries[index]["finished_at"],
                    }
                )
        if rows:
            valid, excluded = accuracy.valid_cases(rows, cases)
            rep["validity"] = {"valid": len(valid), "excluded": excluded}
            rep["accuracy"] = accuracy.summarize(rows, valid)
            rep["backing"] = backing.summarize(rows)
            with (target / f"cases-rep{n}.jsonl").open("w") as handle:
                for row in rows:
                    handle.write(json.dumps(row.dump(), ensure_ascii=False) + "\n")
        (target / f"rep-{n}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
        reps.append(rep)

    summary = dict(summaries[0])
    summary["run_id"] = run_id
    summary["started_at"] = min(s["started_at"] for s in summaries)
    summary["finished_at"] = max(s["finished_at"] for s in summaries)
    versions: dict[str, Any] = {}
    for s in summaries:
        versions.update({k: v for k, v in s["versions"].items() if v is not None and k not in versions})
    summary["versions"] = versions
    summary["dataset"] = {
        **summaries[0]["dataset"],
        "valid": _across([r.get("validity", {}).get("valid") for r in reps]),
        "excluded": [r.get("validity", {}).get("excluded", []) for r in reps],
    }
    summary["combined_from"] = sources
    summary["metrics"] = aggregate(reps)
    (target / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return target


def _across(values: list[Any]) -> dict[str, Any]:
    from niadra_bench import stats

    return stats.across(values, 0)
