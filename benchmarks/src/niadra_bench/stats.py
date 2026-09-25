"""Percentiles and the summary across repetitions (median, and the smallest and largest run)."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from typing import Any


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest rank on the sorted values, the method of niadra-infra's scripts/latency.sh."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[index]


def distribution(values: Sequence[float], digits: int = 1) -> dict[str, Any]:
    """n, p50, p90, p95, p99 and max of one repetition."""
    out: dict[str, Any] = {"n": len(values)}
    for name, p in (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99)):
        value = percentile(values, p)
        out[name] = None if value is None else round(value, digits)
    out["max"] = round(max(values), digits) if values else None
    return out


def rate(hits: int, total: int, digits: int = 4) -> float | None:
    return round(hits / total, digits) if total else None


def across(runs: Sequence[float | None], digits: int = 2) -> dict[str, Any]:
    """What the site shows for a metric: the median of the repetitions and the range between them."""
    values = [v for v in runs if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not values:
        return {"median": None, "min": None, "max": None, "runs": list(runs)}
    return {
        "median": round(statistics.median(values), digits),
        "min": round(min(values), digits),
        "max": round(max(values), digits),
        "runs": [None if v is None else round(v, digits) for v in runs],
    }
