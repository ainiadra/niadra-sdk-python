"""One batch in flight per client: `flush()` and `close()` never overtake the background sender.

A `flush()` that sent the rest at once while the background sender had a batch in flight would
overlap two `/v1/batch` requests, and a `conversation.ended` could land before the turns queued
ahead of it, which reopens the session on the server.
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
from niadra.options import QueueOptions
from tests.conftest import BASE, KEY, batch_ok

URL = f"{BASE}/v1/batch"
# Turns leave on their own almost at once; everything else waits for flush().
EAGER_TURNS = QueueOptions(batch_size=100, interval=3600, turn_interval=0.01)


def turn(text: str) -> dict[str, Any]:
    return {
        "conversation_id": "wa-1",
        "speaker": {"role": "customer"},
        "handles": [{"type": "email", "value": "m@x.co"}],
        "content": {"text": text},
    }


ENDED = {"type": "conversation.ended", "conversation_id": "wa-1"}


def kinds(request: httpx.Request) -> list[str]:
    return [item.get("type", "event") for item in json.loads(request.content)["items"]]


class SlowServer:
    """A `/v1/batch` whose first answer takes `delay` seconds. It records how many requests
    overlapped and the order in which they were answered."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.calls = 0
        self.in_flight = 0
        self.most_in_flight = 0
        self.cancelled = 0
        self.answered: list[list[str]] = []
        self.first_started = threading.Event()

    def _arrive(self) -> int:
        with self.lock:
            self.calls += 1
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
            self.first_started.set()
            return self.calls

    def _leave(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.in_flight -= 1
            self.answered.append(kinds(request))
        return httpx.Response(200, json=batch_ok())

    def sync(self, request: httpx.Request) -> httpx.Response:
        if self._arrive() == 1:
            time.sleep(self.delay)
        return self._leave(request)

    async def asynchronous(self, request: httpx.Request) -> httpx.Response:
        if self._arrive() == 1:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                with self.lock:
                    self.in_flight -= 1
                    self.cancelled += 1
                raise
        return self._leave(request)


def test_flush_waits_for_the_batch_in_flight_and_keeps_the_order(respx_mock: respx.MockRouter) -> None:
    server = SlowServer(delay=0.3)
    respx_mock.post(URL).mock(side_effect=server.sync)
    niadra = Niadra(KEY, channel="whatsapp", queue=EAGER_TURNS)
    niadra._closed = True
    niadra.track(turn("I was charged twice"))
    niadra.track(turn("on the 12th"))
    assert server.first_started.wait(2.0), "the background sender took the turns"
    niadra.track(ENDED)
    assert niadra.flush()
    assert server.most_in_flight == 1
    assert server.answered == [["event", "event"], ["conversation.ended"]]
    assert niadra.pending == 0


def test_flush_with_an_empty_queue_still_waits_for_the_batch_in_flight(respx_mock: respx.MockRouter) -> None:
    server = SlowServer(delay=0.3)
    respx_mock.post(URL).mock(side_effect=server.sync)
    niadra = Niadra(KEY, channel="whatsapp", queue=EAGER_TURNS)
    niadra._closed = True
    niadra.track(turn("I was charged twice"))
    assert server.first_started.wait(2.0)
    assert niadra.pending == 0
    assert niadra.flush()
    assert server.answered == [["event"]], "flush returned only once the turn was answered"


def test_a_flush_timeout_bounds_the_wait_for_the_batch_in_flight(respx_mock: respx.MockRouter) -> None:
    server = SlowServer(delay=0.5)
    respx_mock.post(URL).mock(side_effect=server.sync)
    niadra = Niadra(KEY, channel="whatsapp", queue=EAGER_TURNS)
    niadra._closed = True
    niadra.track(turn("I was charged twice"))
    assert server.first_started.wait(2.0)
    niadra.track(ENDED)
    started = time.monotonic()
    assert not niadra.flush(timeout=0.05)
    assert time.monotonic() - started < 0.3
    assert niadra.pending == 1, "the ended item waits; it was not sent beside the turn"
    assert niadra.flush()
    assert server.most_in_flight == 1
    assert server.answered == [["event"], ["conversation.ended"]]


def test_close_waits_for_the_batch_in_flight_and_keeps_the_order(respx_mock: respx.MockRouter) -> None:
    server = SlowServer(delay=0.3)
    respx_mock.post(URL).mock(side_effect=server.sync)
    niadra = Niadra(KEY, channel="whatsapp", queue=EAGER_TURNS)
    niadra.track(turn("I was charged twice"))
    assert server.first_started.wait(2.0)
    niadra.track(ENDED)
    niadra.close()
    assert server.most_in_flight == 1
    assert server.answered == [["event"], ["conversation.ended"]]


async def test_async_flush_waits_for_the_batch_in_flight_and_keeps_the_order(
    respx_mock: respx.MockRouter,
) -> None:
    server = SlowServer(delay=0.3)
    respx_mock.post(URL).mock(side_effect=server.asynchronous)
    client = AsyncNiadra(KEY, channel="whatsapp", queue=EAGER_TURNS)
    client.track(turn("I was charged twice"))
    client.track(turn("on the 12th"))
    assert await asyncio.to_thread(server.first_started.wait, 2.0)
    client.track(ENDED)
    assert await client.flush()
    assert server.most_in_flight == 1
    assert server.answered == [["event", "event"], ["conversation.ended"]]
    await client.close()


async def test_async_flush_timeout_bounds_the_wait_for_the_batch_in_flight(
    respx_mock: respx.MockRouter,
) -> None:
    server = SlowServer(delay=0.5)
    respx_mock.post(URL).mock(side_effect=server.asynchronous)
    client = AsyncNiadra(KEY, channel="whatsapp", queue=EAGER_TURNS)
    client.track(turn("I was charged twice"))
    assert await asyncio.to_thread(server.first_started.wait, 2.0)
    client.track(ENDED)
    started = time.monotonic()
    assert not await client.flush(timeout=0.05)
    assert time.monotonic() - started < 0.3
    assert client.pending == 1
    await client.close()
    assert server.answered == [["event"], ["conversation.ended"]]


async def test_async_close_lets_the_batch_in_flight_finish_instead_of_cancelling_it(
    respx_mock: respx.MockRouter,
) -> None:
    server = SlowServer(delay=0.3)
    respx_mock.post(URL).mock(side_effect=server.asynchronous)
    client = AsyncNiadra(KEY, channel="whatsapp", queue=EAGER_TURNS)
    client.track(turn("I was charged twice"))
    assert await asyncio.to_thread(server.first_started.wait, 2.0)
    client.track(ENDED)
    await client.close()
    assert server.cancelled == 0, "a cancelled request may still land after the ones behind it"
    assert server.most_in_flight == 1
    assert server.answered == [["event"], ["conversation.ended"]]
