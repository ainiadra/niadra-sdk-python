"""`summary.md`: a run's accuracy by category for every system, for people to read beside `summary.json`.

Wherever it compares Mem0 with Niadra it shows both of Mem0's conditions side by side (estudo 17, WP-11):
`shared id`, where the application already knows who the customer is and sends one user id on every
channel (the harness's `known_id`, Mem0's best case), and `id per channel`, where each channel sends its
own identifier, as agents from different vendors do (`per_channel_id`). Neither alone is the comparison:
the first is what Mem0 does when someone else solved identity, the second what it does when nobody did.

Below the tables, Niadra's lost answers by cause, from the exclusion manifests the run recorded
(`metrics/exclusions.py`): `withheld` (the pack held an item back by policy or verification),
`not_withheld` and `unknown`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from niadra_bench.dataset.model import CATEGORIES

SCENARIO_NAMES = {"known_id": "shared id", "per_channel_id": "id per channel"}
REFERENCES = ("no_memory", "full_history")


def _columns(results: Sequence[Mapping[str, Any]]) -> list[tuple[str, str | None]]:
    """Niadra first, then each system with its conditions next to each other, the references last."""
    keys = [(str(r["system"]), r.get("scenario")) for r in results]

    def order(key: tuple[str, str | None]) -> tuple[int, str, int]:
        system, scenario = key
        rank = 0 if system == "niadra" else 2 if system in REFERENCES else 1
        return rank, system, list(SCENARIO_NAMES).index(scenario) if scenario in SCENARIO_NAMES else -1

    return sorted(dict.fromkeys(keys), key=order)


def _label(system: str, scenario: str | None) -> str:
    return f"{system} ({SCENARIO_NAMES.get(scenario, scenario)})" if scenario else system


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _median(stat: Any) -> Any:
    return stat.get("median") if isinstance(stat, Mapping) else stat


def _table(results: Sequence[Mapping[str, Any]], field: str) -> list[str]:
    columns = _columns(results)
    by_key = {(str(r["system"]), r.get("scenario")): r for r in results}
    lines = [
        "| Category | " + " | ".join(_label(*c) for c in columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    first = next(iter(results), {})
    categories = [c for c in CATEGORIES if c in (first.get("by_category") or {})]
    for category in [*categories, None]:
        cells = []
        for key in columns:
            row = by_key[key]
            if category is None:
                cells.append(_pct(_median(row.get(field))))
            else:
                slot = (row.get("by_category") or {}).get(category) or {}
                cells.append(_pct(_median(slot.get(field))))
        lines.append(f"| {category or 'all'} | " + " | ".join(cells) + " |")
    return lines


def attribution(reps: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Niadra's lost answers by cause, summed over the repetitions that recorded manifests."""
    found = [r["exclusions"]["attribution"] for r in reps if (r.get("exclusions") or {}).get("attribution")]
    if not found:
        return None
    lost: Counter[str] = Counter()
    by_category: dict[str, Counter[str]] = {}
    for one in found:
        lost.update(one.get("lost") or {})
        for category, causes in (one.get("lost_by_category") or {}).items():
            by_category.setdefault(category, Counter()).update(causes)
    return {
        "repetitions": len(found),
        "answers": sum(one.get("answers", 0) for one in found),
        "with_manifest": sum(one.get("with_manifest", 0) for one in found),
        "lost": dict(lost),
        "lost_by_category": {c: dict(v) for c, v in sorted(by_category.items())},
    }


def markdown(summary: Mapping[str, Any], reps: Sequence[Mapping[str, Any]] = ()) -> str:
    env = summary.get("environment") or {}
    lines = [
        f"# Run {summary.get('run_id')}",
        "",
        f"- Where: `{env.get('kind')}`; dataset {(summary.get('dataset') or {}).get('version', '')}; "
        "each figure is the median over the repetitions.",
        "- Mem0 appears in both conditions: `shared id` (one user id on every channel, its best case) and "
        "`id per channel` (each channel sends its own identifier).",
    ]
    results = ((summary.get("metrics") or {}).get("accuracy") or {}).get("results") or []
    if results:
        lines += ["", "## Accuracy by category (judge)", "", *_table(results, "judge")]
        lines += ["", "## Accuracy by category (exact check)", "", *_table(results, "deterministic")]
    found = attribution(reps)
    if found:
        causes = ("withheld", "not_withheld", "unknown")
        lines += [
            "",
            "## Niadra's lost answers by cause",
            "",
            f"From the exclusion manifests of {found['with_manifest']} of {found['answers']} answers over "
            f"{found['repetitions']} repetition(s). `withheld`: the pack held an item back by policy or "
            "verification; `not_withheld`: nothing was held back; `unknown`: no manifest.",
            "",
            "| Category | " + " | ".join(causes) + " |",
            "|---|---|---|---|",
        ]
        for category, counts in [*found["lost_by_category"].items(), ("all", found["lost"])]:
            lines.append(f"| {category} | " + " | ".join(str(counts.get(c, 0)) for c in causes) + " |")
    return "\n".join(lines) + "\n"
