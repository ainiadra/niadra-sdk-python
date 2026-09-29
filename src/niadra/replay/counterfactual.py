"""The measure the tool counterfactual reports (`spec/counterfactual.md`, 4): how much two ranked lists of
object references agree in their first positions, weighted by the exposure a position gets.

The runner of the counterfactual (each recorded tool call run live with and without an element of the
constraints block) reports only positions and these overlaps, never items.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

MAX_K = 100


def overlap_at_k(a: Sequence[str], b: Sequence[str], k: int) -> float:
    """Depth-weighted average overlap of `a` and `b` down to `min(k, the longer list's length)`, with
    `w(d) = 1 / log2(d + 1)`. An item repeated within a list counts once, at its first position. Two equal
    lists overlap 1, and so do two empty lists; a list against an empty one overlaps 0."""
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k must be 1 to {MAX_K}")
    first, second = _once(a), _once(b)
    depth = min(k, max(len(first), len(second)))
    if depth == 0:
        return 1.0
    weighted = total = 0.0
    for d in range(1, depth + 1):
        weight = 1 / math.log2(d + 1)
        weighted += weight * len(set(first[:d]) & set(second[:d])) / d
        total += weight
    return weighted / total


def _once(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
