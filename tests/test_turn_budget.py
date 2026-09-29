"""What capture costs the agent: a tool call recorded inside a turn, against the same call outside one.

The budget is 2 ms at the 95th percentile per tool call, and 5 ms per turn, on the median and the 95th
percentile result sizes seen in the field (25 KB and 150 KB). Digests, the claim check and sending run on the
sender, so what the agent pays is the copy of the arguments and the result, and the bookkeeping.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from niadra.options import TurnOptions
from niadra.turns import TurnRecorder, tool
from niadra.turns.capture import TurnFrame

CALLS = 400


def _result(kilobytes: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    while len(items) * 200 < kilobytes * 1024:
        n = len(items)
        items.append(
            {
                "sku": f"SKU-{n:05d}",
                "name": f"Running shoe model {n} with extra cushioning",
                "price": 199.9 + n,
                "sizes": [36, 37, 38, 39, 40],
                "stock": {"north": n % 7, "south": 0},
                "available": n % 3 != 0,
            }
        )
    return {"results": items, "total": len(items)}


def _p95(samples: list[float]) -> float:
    return sorted(samples)[int(len(samples) * 0.95)]


@pytest.mark.parametrize("kilobytes", [25, 150])
def test_a_recorded_tool_call_costs_under_2_ms_at_the_95th_percentile(kilobytes: int) -> None:
    result = _result(kilobytes)

    @tool("search")
    def search(query: str, page: int) -> dict[str, Any]:
        return result

    recorder = TurnRecorder(TurnOptions(), enabled=True)
    samples: list[float] = []
    for i in range(CALLS):
        frame = recorder.open(conversation_id="c-bench")
        with frame:
            started = time.perf_counter()
            search("running shoes", i)
            samples.append(time.perf_counter() - started)
        recorder.queue.take(50)
    bare: list[float] = []
    for i in range(CALLS):
        started = time.perf_counter()
        search("running shoes", i)
        bare.append(time.perf_counter() - started)
    cost = _p95(samples) - _p95(bare)
    print(f"\n{kilobytes} KB result: capture p95 {cost * 1000:.3f} ms per call")
    assert cost < 0.002


def test_a_turn_costs_under_5_ms_at_the_95th_percentile() -> None:
    result = _result(25)
    recorder = TurnRecorder(TurnOptions(), enabled=True)
    samples: list[float] = []
    for i in range(CALLS):
        started = time.perf_counter()
        with TurnFrame(recorder, agent="bench", conversation_id="c-bench") as frame:
            for k in range(2):
                with frame.tool_call("search", {"query": "shoes", "page": i + k}) as call:
                    call.result(result)
            frame.say("Found 3 options for you.", event_key=f"e-{i}")
        samples.append(time.perf_counter() - started)
        recorder.queue.take(50)
    print(f"\nturn with 2 calls of 25 KB: p95 {_p95(samples) * 1000:.3f} ms")
    assert _p95(samples) < 0.005
