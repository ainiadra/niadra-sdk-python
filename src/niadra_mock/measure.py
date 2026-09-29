"""The tool counterfactual in the emulator (`spec/counterfactual.md`, 6 and 7).

`POST /v1/measure/counterfactual-runs` keeps the report of a runner's cases, computed as the spec says, and
the two `GET` routes read the reports back, newest first. They need the `measurement` feature. The report
keeps no turn or call id."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from niadra.models.signals import CounterfactualRunCreate

ALWAYS = ["not_quality", "model_reaction_not_measured"]


@dataclass
class MeasureStore:
    clock: Callable[[], datetime]
    runs: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def counterfactual(self, body: Mapping[str, Any]) -> dict[str, Any]:
        run = CounterfactualRunCreate.model_validate(body).model_dump(mode="json")
        report = {
            "run_id": str(uuid4()),
            "tool": run["tool"],
            "element": run["element"],
            "label": run.get("label"),
            "created_at": self.clock().isoformat(),
            **summarize(run["cases"], run["element"]),
        }
        with self._lock:
            self.runs.insert(0, report)
        return report

    def listed(self) -> dict[str, Any]:
        with self._lock:
            return {"items": list(self.runs), "next_cursor": None}

    def read(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((r for r in self.runs if r["run_id"] == run_id), None)


def summarize(cases: Sequence[Mapping[str, Any]], element: str) -> dict[str, Any]:
    done = [c for c in cases if c["status"] == "completed"]
    skipped: dict[str, int] = {}
    for case in cases:
        if case["status"] != "completed":
            skipped[case["status"]] = skipped.get(case["status"], 0) + 1
    overlap = _mean([c["overlap"] for c in done])
    noise = _mean([c["noise"] for c in done])
    below = sum(c["overlap"] < c["noise"] for c in done)
    above = sum(c["overlap"] > c["noise"] for c in done)
    limits = list(ALWAYS)
    if element in ("constraints", "hard"):
        limits.append("trivial_for_hard")
    if len(done) < 30:
        limits.append("few_cases")
    if noise is not None and noise < 0.9:
        limits.append("noisy_tool")
    if skipped:
        limits.append("cases_skipped")
    if any(c.get("dry_run") for c in done):
        limits.append("dry_run")
    return {
        "cases": len(cases),
        "completed": len(done),
        "skipped": skipped,
        "overlap": overlap,
        "noise_floor": noise,
        "effect": round(noise - overlap, 6) if overlap is not None and noise is not None else None,
        "below_noise": below,
        "above_noise": above,
        "ties": len(done) - below - above,
        "p_value": _sign_test(below, above),
        "engaged": _engagement(done),
        "limits": limits,
    }


def _engagement(done: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    items = shown = kept = lost = gained = 0
    shifts: list[float] = []
    for case in done:
        k = case["k"]
        for item in case.get("engaged") or []:
            base, variant = item.get("base"), item.get("variant")
            items += 1
            in_base = base is not None and base <= k
            in_variant = variant is not None and variant <= k
            shown += in_base
            kept += in_base and in_variant
            lost += in_base and not in_variant
            gained += in_variant and not in_base
            if base is not None and variant is not None:
                shifts.append(variant - base)
    return {
        "items": items,
        "shown": shown,
        "kept": kept,
        "lost": lost,
        "gained": gained,
        "mean_shift": _mean(shifts),
    }


def _sign_test(below: int, above: int) -> float | None:
    n = below + above
    if n == 0:
        return None
    k = min(below, above)
    if n <= 1000:
        return float(min(1.0, round(2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n, 6)))
    z = (abs(below - above) - 1) / math.sqrt(n)
    return min(1.0, round(math.erfc(z / math.sqrt(2)), 6))


def _mean(values: Sequence[float]) -> float | None:
    return round(math.fsum(values) / len(values), 6) if values else None
