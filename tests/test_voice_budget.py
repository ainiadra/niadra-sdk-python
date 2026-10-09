"""A voice read keeps its turn's budget whatever the connection (benchmark of 09/10/2026, memory slowed
to 2 s): the cold-connection allowance (`Timeouts.connect`) took the first voice read of a new client
to 1.2 s, past a 1 s voice turn. Chat and task reads keep the allowance and the first-read budget, which
fixed first reads that timed out while their pack compiled."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from niadra import AsyncNiadra, CacheOptions
from niadra._transport import Request, Warmth, voiced
from tests.conftest import KEY
from tests.test_voice import MARINA, QUIET


def slow(seconds: float) -> httpx.MockTransport:
    """Every request waits `seconds`, the probe included: the fault proxy of the benchmark."""

    async def handle(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(seconds)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"system_block": "Marina prefere WhatsApp.", "turn_block": None})

    return httpx.MockTransport(handle)


def test_the_allowance_never_takes_a_request_past_its_ceiling() -> None:
    warmth = Warmth(keepalive_s=30.0, allowance=1.0)
    chat = Request("POST", "/v1/context", timeout=0.3, budget=0.3)
    voice = voiced(Request("POST", "/v1/context", timeout=0.2, budget=0.2), True)
    assert warmth.budgeted(chat).budget == pytest.approx(1.3)
    assert warmth.budgeted(voice).budget == pytest.approx(0.2)
    assert voiced(chat, False).ceiling is None


async def test_a_voice_read_of_a_new_client_answers_within_the_voice_budget_when_memory_is_slow() -> None:
    http = httpx.AsyncClient(transport=slow(2.0))
    niadra = AsyncNiadra(KEY, queue=QUIET, http_client=http, cache=CacheOptions(enabled=False))
    try:
        started = time.perf_counter()
        context = await niadra.context(MARINA, view="voice", verification="V1", conversation_id="call-1")
        elapsed = time.perf_counter() - started
    finally:
        await niadra.close(timeout=0)
        await http.aclose()
    assert context.error is not None, "a 2 s memory cannot answer a voice turn"
    assert elapsed < 0.5, f"{elapsed:.3f} s: a voice read never takes the cold-connection allowance"


async def test_a_voice_search_of_a_new_client_answers_within_the_voice_budget_when_memory_is_slow() -> None:
    http = httpx.AsyncClient(transport=slow(2.0))
    niadra = AsyncNiadra(KEY, queue=QUIET, http_client=http)
    try:
        started = time.perf_counter()
        result = await niadra.search(MARINA, "pedido", voice=True)
        elapsed = time.perf_counter() - started
    finally:
        await niadra.close(timeout=0)
        await http.aclose()
    assert result.error is not None
    assert elapsed < 0.6, f"{elapsed:.3f} s: a voice search keeps `navigation_voice`"


async def test_a_chat_read_of_a_new_client_still_gets_the_first_read_and_connection_allowances() -> None:
    http = httpx.AsyncClient(transport=slow(0.9))
    niadra = AsyncNiadra(
        KEY, channel="chat", queue=QUIET, http_client=http, cache=CacheOptions(enabled=False)
    )
    try:
        context = await niadra.context(MARINA, conversation_id="c-1")
    finally:
        await niadra.close(timeout=0)
        await http.aclose()
    assert context.error is None, (
        "0.9 s for a first chat read: within the first-read and connection allowances"
    )
