"""A turn leaves at once by default, and a burst still goes out as few batches.

The only thing that coalesces turns is the one-batch-in-flight rule: what is queued while a batch
is answered leaves together as the next one. So the request rate is bounded by the round trip,
never by the rate of turns, and the order of what was queued is kept.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx
import respx

from niadra import AsyncNiadra, Niadra
from niadra._queue import EventBuffer, _Pacing, _sleep_for
from niadra.options import QueueOptions
from tests.conftest import BASE, KEY, batch_ok

URL = f"{BASE}/v1/batch"


def turn(text: str, conversation: str = "wa-1") -> dict[str, Any]:
    return {
        "conversation_id": conversation,
        "speaker": {"role": "customer"},
        "handles": [{"type": "email", "value": "m@x.co"}],
        "content": {"text": text},
    }


class Server:
    """A `/v1/batch` that answers every request after `delay` seconds and records what it saw."""

    def __init__(self, delay: float, fail_first: int = 0) -> None:
        self.delay = delay
        self.fail_first = fail_first
        self.lock = threading.Lock()
        self.calls = 0
        self.in_flight = 0
        self.most_in_flight = 0
        self.arrived: list[float] = []
        self.delivered: list[str] = []
        self.batches: list[int] = []

    def _arrive(self) -> int:
        with self.lock:
            self.calls += 1
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
            self.arrived.append(time.monotonic())
            return self.calls

    def _leave(self, request: httpx.Request, call: int) -> httpx.Response:
        with self.lock:
            self.in_flight -= 1
            if call <= self.fail_first:
                return httpx.Response(503)
            items = json.loads(request.content)["items"]
            self.batches.append(len(items))
            self.delivered += [i["content"]["text"] for i in items if i.get("type", "event") == "event"]
        return httpx.Response(200, json=batch_ok(len(items)))

    def sync(self, request: httpx.Request) -> httpx.Response:
        call = self._arrive()
        time.sleep(self.delay)
        return self._leave(request, call)

    async def asynchronous(self, request: httpx.Request) -> httpx.Response:
        call = self._arrive()
        await asyncio.sleep(self.delay)
        return self._leave(request, call)


def wait_until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


async def async_wait_until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met in time")


def test_a_turn_leaves_at_once(respx_mock: respx.MockRouter) -> None:
    server = Server(delay=0.0)
    respx_mock.post(URL).mock(side_effect=server.sync)
    niadra = Niadra(KEY, channel="whatsapp")
    try:
        niadra.track(turn("warm the connection"))
        wait_until(lambda: server.delivered == ["warm the connection"])
        started = time.monotonic()
        niadra.track(turn("my new order number is 123456"))
        wait_until(lambda: server.calls == 2)
        assert server.arrived[-1] - started < 0.05, "no queue wait before a turn leaves"
    finally:
        niadra.close()


async def test_an_async_turn_leaves_at_once(respx_mock: respx.MockRouter) -> None:
    server = Server(delay=0.0)
    respx_mock.post(URL).mock(side_effect=server.asynchronous)
    client = AsyncNiadra(KEY, channel="whatsapp")
    client.track(turn("warm the connection"))
    await async_wait_until(lambda: server.delivered == ["warm the connection"])
    started = time.monotonic()
    client.track(turn("my new order number is 123456"))
    await async_wait_until(lambda: server.calls == 2)
    assert server.arrived[-1] - started < 0.05, "no queue wait before a turn leaves"
    await client.close()


def test_a_burst_of_turns_goes_out_as_few_batches_in_order(respx_mock: respx.MockRouter) -> None:
    server = Server(delay=0.1)
    respx_mock.post(URL).mock(side_effect=server.sync)
    niadra = Niadra(KEY, channel="whatsapp")
    try:
        texts = [f"turn {i}" for i in range(40)]
        for text in texts:
            niadra.track(turn(text))
        wait_until(lambda: server.delivered == texts)
        assert server.calls <= 3, f"{server.calls} requests for a burst of 40 turns"
        assert server.most_in_flight == 1
    finally:
        niadra.close()


async def test_an_async_burst_of_turns_goes_out_as_few_batches_in_order(respx_mock: respx.MockRouter) -> None:
    server = Server(delay=0.1)
    respx_mock.post(URL).mock(side_effect=server.asynchronous)
    client = AsyncNiadra(KEY, channel="whatsapp")
    texts = [f"turn {i}" for i in range(40)]
    for text in texts:
        client.track(turn(text))
    await async_wait_until(lambda: server.delivered == texts)
    assert server.calls <= 3, f"{server.calls} requests for a burst of 40 turns"
    assert server.most_in_flight == 1
    await client.close()


async def test_at_25_turns_a_second_the_requests_are_bounded_by_the_round_trip(
    respx_mock: respx.MockRouter,
) -> None:
    """25 turns a second for one second against an API that answers in 100 ms: at most one request
    per round trip (about ten), one in flight, every turn once and in order."""
    server = Server(delay=0.1)
    respx_mock.post(URL).mock(side_effect=server.asynchronous)
    client = AsyncNiadra(KEY, channel="whatsapp")
    texts = [f"turn {i}" for i in range(25)]
    started = time.monotonic()
    for i, text in enumerate(texts):
        await asyncio.sleep(max(0.0, started + i / 25 - time.monotonic()))
        client.track(turn(text, conversation=f"wa-{i % 5}"))
    await async_wait_until(lambda: server.delivered == texts)
    elapsed = time.monotonic() - started
    assert server.most_in_flight == 1
    assert server.calls <= elapsed / 0.1 + 2, f"{server.calls} requests in {elapsed:.2f} s"
    assert server.calls < len(texts)
    await client.close()


def test_a_failing_api_is_retried_after_a_pause_not_in_a_loop(respx_mock: respx.MockRouter) -> None:
    """Three attempts fail (one delivery), then the batch waits in the queue for the pause (the
    interval, 1 s by default) before the next delivery: no turn is lost, none is repeated."""
    server = Server(delay=0.0, fail_first=3)
    respx_mock.post(URL).mock(side_effect=server.sync)
    niadra = Niadra(KEY, channel="whatsapp")
    try:
        niadra.track(turn("I was charged twice"))
        wait_until(lambda: server.calls == 3)
        niadra.track(turn("on the 12th"))
        time.sleep(0.5)
        assert server.calls == 3, "the pause holds, even for a new turn"
        wait_until(lambda: server.delivered == ["I was charged twice", "on the 12th"], timeout=3.0)
        assert server.calls == 4
        assert niadra.pending == 0
    finally:
        niadra.close()


def test_turns_queued_behind_a_batch_leave_right_after_its_answer(respx_mock: respx.MockRouter) -> None:
    server = Server(delay=0.15)
    respx_mock.post(URL).mock(side_effect=server.sync)
    niadra = Niadra(KEY, channel="whatsapp", queue=QueueOptions())
    try:
        niadra.track(turn("first"))
        wait_until(lambda: server.calls == 1)
        niadra.track(turn("second"))
        wait_until(lambda: server.calls == 2)
        gap = server.arrived[1] - server.arrived[0]
        assert 0.15 <= gap < 0.19, f"the second batch left {gap:.3f} s after the first"
        assert niadra.flush()
        assert server.delivered == ["first", "second"]
    finally:
        niadra.close()


def test_a_due_batch_is_not_held_by_the_idle_floor() -> None:
    """The flusher sleeps at least 10 ms only when nothing is due; a due batch leaves now, and a
    failure's pause still holds it."""
    buffer = EventBuffer(QueueOptions())
    pacing = _Pacing(1.0)
    assert _sleep_for(pacing, buffer) == 1.0, "idle: sleeps toward the interval"
    buffer.put(turn("due"))
    assert _sleep_for(pacing, buffer) == 0
    pacing.failed()
    assert 0.9 < _sleep_for(pacing, buffer) <= 1.0
    other = EventBuffer(QueueOptions())
    other.put({"speaker": {"role": "customer"}, "content": {"text": "outside a conversation"}})
    assert 0.01 <= _sleep_for(_Pacing(1.0), other) <= 1.0
