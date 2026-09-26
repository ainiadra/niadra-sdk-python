"""Values without a source per thousand answers: for every agent answer of the accuracy pass, for every
system, the numbers, dates, codes and amounts the answer states that nothing the agent had backs.

"What the agent had" is the same for every system: the memory block that system returned for the probe
(for the references, nothing, or the whole history) and the customer's words in the turn (the probe
question). The dataset's probes call no tool, so there are no tool results to add. The rule is the SDK's
backed-answers check (`niadra.backing.check`: amounts, dates, codes of letters with three or more digits,
numbers of three or more digits; never words, one or two digits, times or a year alone; sums and counts of
backed amounts are backed), run by the harness on every system's answers alike, so it measures the
memory, not an SDK: no system's agent runs it.

Each line gives the answers checked, the values they stated, how many of those had no source and that
count per thousand answers, by kind and by category. Answers are every answered probe (valid or not); the
contradiction and privacy figures of the accuracy lines are not changed by it.
"""

from __future__ import annotations

import importlib
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from niadra_bench import stats


def _load_check() -> Callable[..., Any]:
    """`niadra.backing.check` from the installed SDK when it has it, else the harness's verbatim copy."""
    try:
        module = importlib.import_module("niadra.backing")
    except ImportError:
        module = importlib.import_module("niadra_bench.vendor.niadra_backing")
    check: Callable[..., Any] = module.check
    return check


CHECK = _load_check()


def check(answer: str, block: str, question: str) -> tuple[int, list[str]]:
    """How many values the answer states, and the kind of each one no source backs."""
    report = CHECK(answer, [block, question])
    return int(report.checked), [str(v.kind) for v in report.unbacked]


def _per_1000(unbacked: int, answers: int) -> float | None:
    return round(unbacked * 1000 / answers, 1) if answers else None


def summarize(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """One line per system and scenario, from the accuracy pass's answered rows."""
    groups: dict[tuple[str, str | None], list[Any]] = {}
    for row in rows:
        if row.purpose == "answer" and row.backing_checked is not None:
            groups.setdefault((row.system, row.scenario), []).append(row)
    out: list[dict[str, Any]] = []
    for (system, scenario), group in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        kinds: Counter[str] = Counter(k for r in group for k in r.unbacked_kinds or [])
        by_category: dict[str, dict[str, Any]] = {}
        for category in sorted({r.category for r in group}):
            rows_of = [r for r in group if r.category == category]
            unbacked = sum(len(r.unbacked_kinds or []) for r in rows_of)
            by_category[category] = {
                "answers": len(rows_of),
                "unbacked": unbacked,
                "per_1000_answers": _per_1000(unbacked, len(rows_of)),
            }
        unbacked = sum(kinds.values())
        out.append(
            {
                "system": system,
                "scenario": scenario,
                "answers": len(group),
                "values_checked": sum(r.backing_checked for r in group),
                "unbacked": unbacked,
                "answers_with_unbacked": sum(1 for r in group if r.unbacked_kinds),
                "per_1000_answers": _per_1000(unbacked, len(group)),
                "by_kind": dict(sorted(kinds.items())),
                "by_category": by_category,
            }
        )
    return out


def aggregate(reps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The metric across repetitions (the median and the range of the rate), or None when no repetition
    ran it (results from before 26/09/2026)."""
    if not any("backing" in rep for rep in reps):
        return None
    groups: dict[tuple[str, str | None], list[dict[str, Any] | None]] = {}
    for index, rep in enumerate(reps):
        for line in rep.get("backing") or []:
            groups.setdefault((line["system"], line["scenario"]), [None] * len(reps))[index] = line
    rows = []
    for (system, scenario), lines in groups.items():
        present = [line for line in lines if line]
        kinds: Counter[str] = Counter()
        for line in present:
            kinds.update(line.get("by_kind") or {})
        categories = sorted({c for line in present for c in line.get("by_category") or {}})
        rows.append(
            {
                "system": system,
                "scenario": scenario,
                "per_1000_answers": stats.across([(line or {}).get("per_1000_answers") for line in lines], 1),
                "answers": sum(line["answers"] for line in present),
                "unbacked": sum(line["unbacked"] for line in present),
                "by_kind": dict(sorted(kinds.items())),
                "by_category": {
                    category: stats.across(
                        [
                            ((line or {}).get("by_category") or {}).get(category, {}).get("per_1000_answers")
                            for line in lines
                        ],
                        1,
                    )
                    for category in categories
                },
            }
        )
    return {"unit": "values without a source per 1000 answers", "results": rows}
