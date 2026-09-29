"""Percentiles, the summary across repetitions (median, and the smallest and largest run), and the 95%
intervals of a share and of the difference between two shares."""

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


Z95 = 1.959964


def wilson(correct: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """The Wilson score interval of a share: right at 0% and 100%, where the normal one collapses."""
    if not n:
        return None
    p = correct / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def share_difference(
    correct: int, n: int, base_correct: int, base_n: int, z: float = Z95
) -> tuple[float, float] | None:
    """Newcombe's interval (method 10) of `correct/n - base_correct/base_n`, two independent samples, built
    from each share's Wilson interval."""
    one, two = wilson(correct, n, z), wilson(base_correct, base_n, z)
    if one is None or two is None:
        return None
    p1, p2 = correct / n, base_correct / base_n
    (l1, u1), (l2, u2) = one, two
    delta = p1 - p2
    return (
        delta - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
        delta + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2),
    )
