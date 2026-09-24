"""Every call a caller waits on ends within its budget, however slowly the network answers.

httpx times each phase of a request apart, so without a bound of its own a slow answer could
hold the caller for several times the budget. These tests answer late on purpose.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx
import pytest
import respx

from niadra import APITimeoutError, AsyncNiadra, Niadra, phone
from niadra._transport import AsyncTransport, Request, SyncTransport
from niadra.options import QueueOptions, Timeouts
from tests.conftest import KEY, batch_ok, context_payload

MARINA = phone("+5511912345678")
UPLOAD_URL = "https://media.example-bucket.s3.amazonaws.com/sp/med_1?X-Amz-Signature=abc"
QUIET = QueueOptions(batch_size=10_000, interval=3600, turn_interval=3600)
# Its own address: an attempt these tests abandon, or a retry of the background queue, may still
# be in flight when the next test mocks the API, and must never match that test's routes.
BASE = "https://budgets.example.test"


def late(seconds: float, response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        time.sleep(seconds)
        return response

    return answer


def late_async(
    seconds: float, response: httpx.Response
) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    async def answer(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(seconds)
        return response

    return answer


def read(budget: float) -> Request:
    return Request("POST", "/v1/context", json={}, timeout=budget, budget=budget)


def test_a_slow_answer_never_holds_the_caller_past_the_budget(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").mock(side_effect=late(0.6, httpx.Response(200, json={})))
    transport = SyncTransport(BASE, KEY)
    started = time.monotonic()
    with pytest.raises(APITimeoutError):
        transport.request(read(0.15))
    assert time.monotonic() - started < 0.4
    transport.close()


def test_an_answer_within_the_budget_comes_back_whole(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").mock(side_effect=late(0.02, httpx.Response(200, json={"ok": 1})))
    transport = SyncTransport(BASE, KEY)
    assert transport.request(read(1.0)) == {"ok": 1}
    transport.close()


async def test_the_async_transport_cancels_a_slow_attempt_at_the_budget(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").mock(side_effect=late_async(1.0, httpx.Response(200, json={})))
    transport = AsyncTransport(BASE, KEY)
    started = time.monotonic()
    with pytest.raises(APITimeoutError):
        await transport.request(read(0.15))
    assert time.monotonic() - started < 0.4
    await transport.aclose()


def test_context_returns_empty_on_time_when_the_answer_is_late(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").mock(
        side_effect=late(0.8, httpx.Response(200, json=context_payload()))
    )
    niadra = Niadra(KEY, base_url=BASE, timeouts=Timeouts(context=0.2), queue=QUIET)
    niadra._closed = True
    started = time.monotonic()
    context = niadra.context(MARINA, conversation_id="c-1")
    assert time.monotonic() - started < 0.45
    assert not context and context.degraded


def test_identify_stops_waiting_at_the_write_budget_and_keeps_the_item(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/batch").mock(side_effect=late(0.5, httpx.Response(503)))
    niadra = Niadra(KEY, base_url=BASE, channel="whatsapp", timeouts=Timeouts(write=0.3), queue=QUIET)
    niadra._closed = True
    started = time.monotonic()
    result = niadra.identify([MARINA, phone("+5511987654321")], conversation_id="c-1")
    assert time.monotonic() - started < 0.55, "three attempts would take 1.5 s"
    assert result is None
    assert len(niadra._core.buffer) == 1, "the assertion waits in the queue for the background sender"


def test_feedback_stops_waiting_at_the_write_budget(respx_mock: respx.MockRouter) -> None:
    attempts: list[float] = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        attempts.append(time.monotonic())
        time.sleep(0.25)
        return httpx.Response(503)

    respx_mock.post(f"{BASE}/v1/feedback").mock(side_effect=unavailable)
    niadra = Niadra(KEY, base_url=BASE, timeouts=Timeouts(write=0.4), queue=QUIET)
    niadra._closed = True
    started = time.monotonic()
    assert niadra.feedback("retract_fact", MARINA, fact_id="f-1") is None
    assert time.monotonic() - started < 0.65
    assert len(attempts) == 2, "the second attempt is cut at the budget, the third never starts"


def test_the_background_queue_keeps_per_attempt_timeouts(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").mock(
        side_effect=late(0.3, httpx.Response(200, json=batch_ok()))
    )
    niadra = Niadra(KEY, base_url=BASE, channel="whatsapp", timeouts=Timeouts(write=0.2), queue=QUIET)
    niadra._closed = True
    niadra.track(
        {
            "speaker": {"role": "customer"},
            "handles": [{"type": "email", "value": "m@x.co"}],
            "content": {"text": "oi"},
        }
    )
    assert niadra.flush(timeout=2.0)
    assert route.call_count == 1, "a batch nobody waits on is not cut at the write budget"


def test_an_upload_ends_within_its_budget(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/media/uploads").respond(
        201, json={"media_ref": "med_1", "upload_url": UPLOAD_URL, "expires_at": "2026-09-22T14:22:00Z"}
    )
    respx_mock.put(UPLOAD_URL).mock(side_effect=late(0.4, httpx.Response(200)))
    niadra = Niadra(KEY, base_url=BASE, timeouts=Timeouts(upload=0.2), queue=QUIET)
    niadra._closed = True
    started = time.monotonic()
    assert niadra.upload_media(b"RIFF....WAVEfmt ", "audio/wav", subject=MARINA) is None
    assert time.monotonic() - started < 0.45


async def test_async_identify_stops_waiting_at_the_write_budget(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/batch").mock(
        side_effect=late_async(1.0, httpx.Response(200, json=batch_ok()))
    )
    niadra = AsyncNiadra(KEY, base_url=BASE, channel="whatsapp", timeouts=Timeouts(write=0.2), queue=QUIET)
    started = time.monotonic()
    result = await niadra.identify([MARINA, phone("+5511987654321")], conversation_id="c-1")
    assert time.monotonic() - started < 0.45
    assert result is None
    niadra._closed = True
