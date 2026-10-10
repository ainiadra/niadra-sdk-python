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
from niadra._transport import AsyncTransport, Request, SyncTransport, Warmth
from niadra.options import QueueOptions, Timeouts
from tests.conftest import KEY, batch_ok, context_payload

MARINA = phone("+5511912345678")
UPLOAD_URL = "https://media.example-bucket.s3.amazonaws.com/sp/med_1?X-Amz-Signature=abc"
QUIET = QueueOptions(batch_size=10_000, interval=3600, turn_interval=3600)
# Its own address: an attempt these tests abandon, or a retry of the background queue, may still
# be in flight when the next test mocks the API, and must never match that test's routes.
BASE = "https://budgets.example.test"
# The budgets of a client whose connection is open: the allowance for opening one has its own tests.
OPEN = {"connect": 0.0}


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
    niadra = Niadra(KEY, base_url=BASE, timeouts=Timeouts(context=0.2, **OPEN), queue=QUIET)
    niadra._closed = True
    started = time.monotonic()
    context = niadra.context(MARINA, conversation_id="c-1")
    assert time.monotonic() - started < 0.45
    assert not context and context.degraded


def test_identify_stops_waiting_at_the_write_budget_and_keeps_the_item(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/batch").mock(side_effect=late(0.5, httpx.Response(503)))
    niadra = Niadra(KEY, base_url=BASE, channel="whatsapp", timeouts=Timeouts(write=0.3, **OPEN), queue=QUIET)
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
    niadra = Niadra(KEY, base_url=BASE, timeouts=Timeouts(write=0.4, **OPEN), queue=QUIET)
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
    niadra = AsyncNiadra(
        KEY, base_url=BASE, channel="whatsapp", timeouts=Timeouts(write=0.2, **OPEN), queue=QUIET
    )
    started = time.monotonic()
    result = await niadra.identify([MARINA, phone("+5511987654321")], conversation_id="c-1")
    assert time.monotonic() - started < 0.45
    assert result is None
    niadra._closed = True


# Opening a connection (TCP and TLS) takes a few round trips, which a 0.3 s budget cannot hold from another
# continent: the first call with no connection open gets the `connect` allowance, once.


def test_a_read_with_no_open_connection_gets_the_allowance_once(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").mock(side_effect=late(0.3, httpx.Response(200, json={"ok": 1})))
    transport = SyncTransport(BASE, KEY, cold_allowance=0.5)
    assert transport.request(read(0.15)) == {"ok": 1}
    started = time.monotonic()
    with pytest.raises(APITimeoutError):
        transport.request(read(0.15))
    assert time.monotonic() - started < 0.4, "with a connection open, the budget is exact"
    transport.close()


async def test_the_async_transport_gives_the_allowance_once(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").mock(
        side_effect=late_async(0.3, httpx.Response(200, json={"ok": 1}))
    )
    transport = AsyncTransport(BASE, KEY, cold_allowance=0.5)
    assert await transport.request(read(0.15)) == {"ok": 1}
    with pytest.raises(APITimeoutError):
        await transport.request(read(0.15))
    await transport.aclose()


def alone(warmth: Warmth, request: Request) -> Request:
    """`request` as `warmth` budgets it when nothing else is in flight."""
    budgeted = warmth.begin(request)
    warmth.end(budgeted)
    return budgeted


class Ticks:
    """A clock the test moves: `Warmth`'s windows are 50 ms here, and a loaded machine can take that between
    two statements, so the tests read no wall clock."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_the_allowance_comes_back_when_the_connection_was_idle_past_the_keepalive() -> None:
    ticks = Ticks()
    warmth = Warmth(keepalive_s=0.05, allowance=0.5, clock=ticks)
    assert alone(warmth, read(0.1)).budget == pytest.approx(0.6)
    assert alone(warmth, read(0.1)).timeout == pytest.approx(0.6)
    warmth.answered()
    assert alone(warmth, read(0.1)).budget == pytest.approx(0.1)
    ticks.now += 0.06
    assert alone(warmth, read(0.1)).budget == pytest.approx(0.6), "idle past the keepalive: cold again"
    unbudgeted = Request("POST", "/v1/batch", json={}, timeout=5.0)
    assert alone(warmth, unbudgeted) is unbudgeted, "a batch of the background queue keeps its timeouts"
    assert alone(Warmth(keepalive_s=120, allowance=0), read(0.1)).budget == pytest.approx(0.1)


def test_an_outage_spends_the_allowance_once_not_on_every_turn() -> None:
    ticks = Ticks()
    warmth = Warmth(keepalive_s=120, allowance=0.05, clock=ticks)
    assert alone(warmth, read(0.1)).budget == pytest.approx(0.15)
    assert alone(warmth, read(0.1)).budget == pytest.approx(0.15), "a call of the same moment gets it too"
    ticks.now += 0.06
    assert alone(warmth, read(0.1)).budget == pytest.approx(0.1), (
        "no answer came: later turns keep the budget"
    )
    warmth.answered()
    warmth.forget()
    assert alone(warmth, read(0.1)).budget == pytest.approx(0.15), "a new connection gets it again"


def test_a_request_that_starts_while_every_open_connection_is_busy_gets_the_allowance() -> None:
    # juridico-zero, 09/10/2026: the keep-warm ping kept one connection open; a turn read its context, notes
    # and state at once, two of them opened a connection again from Sao Paulo, and the notes' 0.3 s ran out.
    warmth = Warmth(keepalive_s=120, allowance=1.0)
    ping = warmth.begin(read(0.3))
    warmth.answered()  # the ping: one connection
    warmth.end(ping)
    turn = [warmth.begin(read(0.3)) for _ in range(3)]
    assert [r.budget for r in turn] == pytest.approx([0.3, 1.3, 1.3])
    for _ in turn:
        warmth.answered()  # all three were out: three connections answered
    for r in turn:
        warmth.end(r)
    again = [warmth.begin(read(0.3)) for _ in range(3)]
    assert [r.budget for r in again] == pytest.approx([0.3, 0.3, 0.3]), "the pool keeps the three"
    for r in again:
        warmth.end(r)


async def test_reads_that_open_connections_beside_a_warm_one_keep_their_answers(
    respx_mock: respx.MockRouter,
) -> None:
    calls = 0

    async def answer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        n = calls
        # The first of the turn's reads finds the warm connection; the others open one (TCP and TLS).
        await asyncio.sleep(0.05 if n in (1, 2) else 0.5)
        return httpx.Response(200, json={"ok": n})

    respx_mock.post(f"{BASE}/v1/context").mock(side_effect=answer)
    transport = AsyncTransport(BASE, KEY, cold_allowance=1.0)
    assert await transport.request(read(0.3)) == {"ok": 1}  # the connection opens and answers
    answers = await asyncio.gather(*(transport.request(read(0.3)) for _ in range(3)))
    assert sorted(a["ok"] for a in answers) == [2, 3, 4]
    await transport.aclose()


def test_the_first_context_of_a_cold_client_arrives_and_the_next_keeps_the_budget(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(f"{BASE}/v1/context").mock(
        side_effect=late(0.3, httpx.Response(200, json=context_payload()))
    )
    niadra = Niadra(KEY, base_url=BASE, timeouts=Timeouts(context=0.15, connect=0.5), queue=QUIET)
    niadra._closed = True
    assert niadra.context(MARINA, conversation_id="c-1")
    started = time.monotonic()
    late_one = niadra.context(MARINA, conversation_id="c-2")
    assert time.monotonic() - started < 0.4
    assert not late_one and late_one.degraded
