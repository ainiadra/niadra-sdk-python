"""What metrics 8 (history navigation) and 9 (ingestion acknowledgement) share: one API call measured
exactly as metric 1 measures the context, with `latency.open_loop` (requests leave at a constant rate
whatever the answers do, one warm-up call per conversation first, the same HTTP client and limits),
and the lines summarized across repetitions the way the runner summarizes latency.

A call is timed from the moment it leaves until its whole answer is read. Only the answer the caller
waits for counts as a success; everything else (an HTTP error status, a timeout, a refused
connection) is an error, counted by kind. A success that carried nothing (a search with no result)
or a degraded answer (Niadra's `text_only` search, when its encoder did not answer in time) stays in
the percentiles and is counted apart, so a fast empty answer never hides.

When the server names its own steps in a `Server-Timing` header (`app;dur=12.5, encode;dur=8.1`), a line
that records them (`Operation.timings`) also gets their percentiles, per step, beside the client's.
"""

from __future__ import annotations

import contextlib
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from niadra_bench import stats
from niadra_bench.metrics import latency
from niadra_bench.stats import distribution

#: Outcomes of a call that returned what the caller waits for.
OK = "ok"
EMPTY = "empty"
DEGRADED = "degraded"
SUCCESS = frozenset({OK, EMPTY, DEGRADED})

#: Reads the answer of a successful call and says whether it was complete, empty or degraded.
Classify = Callable[[httpx.Response], str]


def plain(_response: httpx.Response) -> str:
    return OK


def server_timing(response: httpx.Response) -> dict[str, float]:
    """The steps a `Server-Timing` header names, in milliseconds (entries without `dur` are left out)."""
    out: dict[str, float] = {}
    for entry in response.headers.get("server-timing", "").split(","):
        name, *params = (part.strip() for part in entry.split(";"))
        for param in params:
            key, _, value = param.partition("=")
            if name and key.strip() == "dur":
                with contextlib.suppress(ValueError):
                    out[name] = float(value.strip().strip('"'))
    return out


def recording(classify: Classify, timings: dict[str, list[float]]) -> Classify:
    """`classify`, also keeping the `Server-Timing` steps of every successful answer in `timings`."""

    def run(response: httpx.Response) -> str:
        for name, ms in server_timing(response).items():
            timings.setdefault(name, []).append(ms)
        return classify(response)

    return run


async def timed(
    send: Awaitable[httpx.Response], expect: int, classify: Classify = plain
) -> tuple[float, str]:
    """Milliseconds until the answer is read, and the outcome: a success kind, `http<status>` or the
    exception's name."""
    started = time.perf_counter()
    try:
        response = await send
    except httpx.HTTPError as exc:
        return (time.perf_counter() - started) * 1000, type(exc).__name__
    elapsed = (time.perf_counter() - started) * 1000
    if response.status_code != expect:
        return elapsed, f"http{response.status_code}"
    try:
        return elapsed, classify(response)
    except ValueError:
        return elapsed, "unreadable"


@dataclass
class Operation:
    """One line of a metric: a system's call over one path."""

    probe: latency.Probe
    operation: str  # search, open, batch, add_raw, add_infer
    call: str  # the HTTP call as the results name it, e.g. "POST /v1/history/search"
    #: Why the line could not be measured (for example, no item to open); it is still written.
    skipped: str | None = None
    #: The server's own steps (`Server-Timing`) per answer, when the line records them (`recording`).
    timings: dict[str, list[float]] = field(default_factory=dict)


def summarize(
    op: Operation, rate: int, duration_s: float, results: Sequence[tuple[float, str]]
) -> dict[str, Any]:
    ok = [ms for ms, outcome in results if outcome in SUCCESS]
    outcomes = Counter(outcome for _, outcome in results)
    errors = {k: v for k, v in sorted(outcomes.items()) if k not in SUCCESS}
    return {
        "system": op.probe.system,
        "path": op.probe.path,
        "operation": op.operation,
        "call": op.call,
        "rate": rate,
        "duration_s": duration_s,
        **distribution(ok),
        "sent": len(results),
        "errors": errors,
        "empty": outcomes.get(EMPTY, 0),
        "degraded": outcomes.get(DEGRADED, 0),
        **({"skipped": op.skipped} if op.skipped else {}),
        **(
            {"server_timing": {name: distribution(v) for name, v in sorted(op.timings.items())}}
            if op.timings
            else {}
        ),
    }


async def measure(
    ops: Sequence[Operation], rates: Sequence[int], duration_s: float, warmups: int, timeout_s: float
) -> list[dict[str, Any]]:
    """One line per system, path, operation and rate, one after the other, never at the same time."""
    out: list[dict[str, Any]] = []
    for op in ops:
        for rate in rates:
            if op.skipped:
                out.append(summarize(op, rate, duration_s, []))
                continue
            op.timings.clear()
            results = await latency.open_loop(op.probe, rate, duration_s, warmups, timeout_s)
            out.append(summarize(op, rate, duration_s, results))
    return out


def _pick(lines: list[dict[str, Any] | None], name: str) -> list[float | None]:
    return [value if isinstance(value := (line or {}).get(name), int | float) else None for line in lines]


def aggregate(reps: list[dict[str, Any]], metric: str) -> dict[str, Any] | None:
    """The summary of one of these metrics across repetitions, or None when no repetition ran it."""
    if not any(metric in rep for rep in reps):
        return None
    groups: dict[tuple[Any, ...], list[dict[str, Any] | None]] = {}
    for index, rep in enumerate(reps):
        for line in rep.get(metric) or []:
            key = (line["system"], line["path"], line["operation"], line["rate"])
            groups.setdefault(key, [None] * len(reps))[index] = line
    rows = []
    for (system, path, operation, rate), lines in groups.items():
        present = [line for line in lines if line]
        kinds: Counter[str] = Counter()
        for line in present:
            kinds.update(line.get("errors") or {})
        skipped = next((line["skipped"] for line in present if line.get("skipped")), None)
        steps = sorted({name for line in present for name in line.get("server_timing") or {}})
        server = {
            name: {
                p: stats.across(
                    [((line or {}).get("server_timing") or {}).get(name, {}).get(p) for line in lines], 1
                )
                for p in ("p50", "p95")
            }
            for name in steps
        }
        rows.append(
            {
                "system": system,
                "path": path,
                "operation": operation,
                "call": present[0].get("call") if present else None,
                "rate": rate,
                **{p: stats.across(_pick(lines, p), 1) for p in ("p50", "p95", "p99", "max")},
                "errors": sum(kinds.values()),
                "error_kinds": dict(sorted(kinds.items())),
                "sent": sum(line.get("sent", 0) for line in present),
                "empty": sum(line.get("empty", 0) for line in present),
                "degraded": sum(line.get("degraded", 0) for line in present),
                **({"skipped": skipped} if skipped else {}),
                **({"server_timing": server} if server else {}),
            }
        )
    return {"unit": "ms", "results": rows}
