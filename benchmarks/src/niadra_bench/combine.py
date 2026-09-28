"""`bench combine`: one results folder from several runs of the same dataset and configuration, each run
with its own systems (the temporary host measures one system at a time, so each has the host alone).

Per repetition, the accuracy pass is summarized again over all the runs' rows with one validity rule:
the two references (no memory, full history) decide which cases count, for every system. They come from
the first run, or from the folder `--references` names (a run whose references were measured on the same
dataset, kept apart from the systems it scored). The other metrics' lines are put together as they are; a
line two runs share (the embedder's `encode` line, Niadra's public prices) is kept once, from the first run
that has it. The combined `summary.json` has the schema the site imports, with the list of runs it came
from.

The runs may differ in repetitions and in cases (`--limit`), since the slow and the costly systems run
fewer. The references may cover fewer repetitions than a system: a repetition past the references' last
takes its validity from that last one, and says so (`validity.references`). A run without references
(`--no-references`) can only come after the run or the folder that has them. Each source in
`combined_from` says how many repetitions and cases it measured, `per_system` gives the same per system,
and `accuracy_shared` scores every system again on the valid cases all of them answered, so a system
measured on fewer cases is set beside the others on the same cases, never on its own.

A source folder may carry `cell-cost.json`: cost lines measured outside the harness for that run (Niadra's
own model spend, read from the cell's ledger over the run's window), with the inputs that give them. They
join that repetition's cost lines like any other.
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


def _repetitions(directory: Path) -> int:
    """How many repetitions a folder holds rows for (a stopped run may have no summary.json)."""
    n = 0
    while (directory / f"cases-rep{n + 1}.jsonl").exists():
        n += 1
    return n


def _check_same(summaries: list[dict[str, Any]], references: dict[str, Any] | None) -> None:
    first = summaries[0]
    if references is None and first["config"].get("references", "on") == "off":
        raise ValueError(f"{first['run_id']} has no references: the first run's decide validity")
    others = summaries[1:] if references is None else summaries
    for other in [*others, *([references] if references is not None and "config" in references else [])]:
        for section, field in (("dataset", "hash"), ("config", "hash")):
            if other[section][field] != first[section][field]:
                raise ValueError(
                    f"{other['run_id']} has another {section} {field} than {first['run_id']}: "
                    "only runs of the same dataset and configuration combine"
                )
    cases = references["dataset"]["cases"] if references is not None and "dataset" in references else None
    for other in others:
        most = cases if cases is not None else first["dataset"]["cases"]
        if other["dataset"]["cases"] > most:
            raise ValueError(
                f"{other['run_id']} has more cases than the references cover: they decide which cases count"
            )


def _references(directory: Path) -> tuple[dict[str, Any], dict[int, list[accuracy.CaseRow]]]:
    """The reference rows of a folder, by repetition, and its summary (or what its folder name gives)."""
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {"run_id": directory.name}
    rows = {
        n: [r for r in _rows(directory, n) if r.system in REFERENCES]
        for n in range(1, _repetitions(directory) + 1)
    }
    rows = {n: found for n, found in rows.items() if found}
    if not rows:
        raise ValueError(f"{directory} has no references: no row of {sorted(REFERENCES)}")
    return summary, rows


def _cell_cost(directory: Path, n: int) -> list[dict[str, Any]]:
    """Cost lines measured outside the harness for repetition n of a run (cell-cost.json), if any."""
    path = directory / "cell-cost.json"
    if not path.exists():
        return []
    lines = json.loads(path.read_text()).get("repetitions", {}).get(str(n), [])
    for line in lines:
        if not {"system", "variant", "memory_usd_per_1000", "basis"} <= line.keys():
            raise ValueError(f"{path}: a cost line needs system, variant, memory_usd_per_1000 and basis")
    return list(lines)


def combine(
    directories: list[Path], output: Path, cases: dict[str, Case], references: Path | None = None
) -> Path:
    from niadra_bench.runner import aggregate

    summaries = [json.loads((d / "summary.json").read_text()) for d in directories]
    ref_summary, ref_rows = _references(references) if references is not None else (None, {})
    _check_same(summaries, ref_summary)
    if references is None:
        _, ref_rows = _references(directories[0])
        ref_source = summaries[0]["run_id"]
    else:
        ref_source = ref_summary["run_id"] if ref_summary else references.name
    repetitions = max(int(s["config"]["repetitions"]) for s in summaries)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    target = output / f"{summaries[0]['date']}-{run_id[-6:]}"
    target.mkdir(parents=True, exist_ok=True)
    reps: list[dict[str, Any]] = []
    rows_by_rep: dict[int, list[accuracy.CaseRow]] = {}
    sources: list[dict[str, Any]] = []
    for n in range(1, repetitions + 1):
        rows: list[accuracy.CaseRow] = list(ref_rows.get(n, []))
        seen_systems: set[tuple[str, str | None]] = {(r.system, r.scenario) for r in rows}
        rep: dict[str, Any] = {"repetition": n, "seed": {}, "settle": {}, "combined": True}
        for index, directory in enumerate(directories):
            if not (directory / f"rep-{n}.json").exists():
                # A run with fewer repetitions (a costly or slow system) counts in its own only.
                continue
            run_rows = _rows(directory, n)
            systems = {(r.system, r.scenario) for r in run_rows}
            # The references come from one place only: the folder that decides validity.
            keep = {s for s in systems if s[0] not in REFERENCES} - seen_systems
            rows += [r for r in run_rows if (r.system, r.scenario) in keep]
            seen_systems |= keep
            raw = json.loads((directory / f"rep-{n}.json").read_text())
            raw["cost"] = [*(raw.get("cost") or []), *_cell_cost(directory, n)]
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
        rows_by_rep[n] = rows
        reps.append(rep)

    # The cases every system answered, in every repetition it ran: the ground all of them share.
    answered: dict[str, set[str]] = {}
    for rows in rows_by_rep.values():
        for r in rows:
            if r.system not in REFERENCES and r.purpose == "answer":
                answered.setdefault(r.system, set()).add(r.case_id)
    shared = set.intersection(*answered.values()) if answered else set()

    last = max(ref_rows)
    for n, rep in enumerate(reps, start=1):
        rows = rows_by_rep[n]
        if not rows:
            (target / f"rep-{n}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
            continue
        from_rep = n if n in ref_rows else last
        valid, excluded = accuracy.valid_cases(ref_rows[from_rep], cases)
        rep["validity"] = {
            "valid": len(valid),
            "excluded": excluded,
            "references": {"run_id": ref_source, "repetition": from_rep},
            "shared_cases": len(shared),
            "shared_valid": len(valid & shared),
        }
        rep["accuracy"] = accuracy.summarize(rows, valid)
        rep["accuracy_shared"] = accuracy.summarize(rows, valid & shared)
        rep["backing"] = backing.summarize(rows)
        with (target / f"cases-rep{n}.jsonl").open("w") as handle:
            for row in rows:
                handle.write(json.dumps(row.dump(), ensure_ascii=False) + "\n")
        (target / f"rep-{n}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")

    summary = dict(summaries[0])
    summary["config"] = {
        **summaries[0]["config"],
        "repetitions": repetitions,
        "references": "on",
        # One smoke run among the sources makes the whole folder a smoke run.
        "quick": any(s["config"].get("quick", False) for s in summaries),
    }
    summary["run_id"] = run_id
    summary["started_at"] = min(s["started_at"] for s in summaries)
    summary["finished_at"] = max(s["finished_at"] for s in summaries)
    versions: dict[str, Any] = {}
    for s in summaries:
        versions.update({k: v for k, v in s["versions"].items() if v is not None and k not in versions})
    summary["versions"] = versions
    summary["dataset"] = {
        **summaries[0]["dataset"],
        "cases": max(s["dataset"]["cases"] for s in summaries),
        "valid": _across([r.get("validity", {}).get("valid") for r in reps]),
        "excluded": [r.get("validity", {}).get("excluded", []) for r in reps],
        "references": {"run_id": ref_source, "repetitions": sorted(ref_rows)},
        "shared": {
            "cases": len(shared),
            "valid": _across([r.get("validity", {}).get("shared_valid") for r in reps]),
        },
    }
    summary["combined_from"] = sources
    summary["per_system"] = {
        system: {"run_id": source["run_id"], "repetitions": source["repetitions"], "cases": source["cases"]}
        for source in sources
        for system in source["systems"]
    }
    summary["metrics"] = aggregate(reps)
    shared_metrics = aggregate([{"accuracy": r["accuracy_shared"]} for r in reps if "accuracy_shared" in r])
    if "accuracy" in shared_metrics:
        summary["metrics"]["accuracy_shared"] = {
            "unit": "share of the valid cases every system answered",
            "results": shared_metrics["accuracy"]["results"],
        }
    (target / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return target


def _across(values: list[Any]) -> dict[str, Any]:
    from niadra_bench import stats

    return stats.across(values, 0)
