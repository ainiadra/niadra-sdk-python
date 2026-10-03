"""The read budgets and the round trip to the region (B22): a default budget is what the API may take, the
measured round trip goes on top, so a caller far from the region (Sao Paulo, 170 ms from us-east-2) is not
timed out by the network; a budget the caller set stays a ceiling, and the SDK says once when the network
alone exceeds it."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest

from niadra import AsyncNiadra, Niadra, Timeouts, VoiceOptions
from niadra._base import ClientCore
from tests.conftest import KEY
from tests.test_voice import MARINA, QUIET, Region


def core(timeouts: Timeouts | None = None) -> ClientCore:
    return ClientCore(KEY, None, channel="chat", strict=False, timeouts=timeouts, cache=None, queue=None)


def test_default_read_budgets_take_the_round_trip_on_top_once_it_is_measured() -> None:
    c = core()
    assert c.context_budget("chat", None) == pytest.approx(0.30)  # no probe sent: the defaults as they are
    c.measuring = True
    # The probe is on its way: `connect` on top, so a read right after the client starts is not cut short.
    assert c.context_budget("chat", None) == pytest.approx(1.30)
    assert c.navigation_budget(False, None) == pytest.approx(1.60)
    assert c.probed([0.42, 0.17]) == pytest.approx(0.17)  # the fastest sample
    assert c.context_budget("chat", None) == pytest.approx(0.47)
    assert c.navigation_budget(False, None) == pytest.approx(0.77)
    assert c.context_budget("chat", 1.2) == pytest.approx(1.2)  # a call's own timeout is its own
    # The voice budgets are not round trips: a voice turn reads a pack already in memory.
    assert c.context_budget("voice", None) == pytest.approx(0.20)
    assert c.navigation_budget(True, None) == pytest.approx(0.30)


def test_a_budget_the_caller_set_stays_a_ceiling_and_is_reported_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="niadra")
    c = core(Timeouts(context=0.2))
    c.probed([0.25])
    assert c.context_budget("chat", None) == pytest.approx(0.2)
    assert c.navigation_budget(False, None) == pytest.approx(0.85)  # still at its default
    [warning] = [r.getMessage() for r in caplog.records]
    assert "Timeouts.context (200 ms) is shorter than the round trip to the region (250 ms)" in warning


def test_the_voice_warnings_wait_for_a_voice_read(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="niadra")
    c = core()
    c.probed([0.3])
    assert not caplog.records, "a client that only chats hears nothing about the voice budgets"
    c.start_voice()
    c.start_voice()
    assert sum("Timeouts.context_voice" in r.getMessage() for r in caplog.records) == 1


async def test_a_read_from_far_away_fits_its_default_budget_after_the_probe() -> None:
    region = Region(latency=(0.4, 0.4))
    http = httpx.AsyncClient(transport=httpx.MockTransport(region.handle_async))
    niadra = AsyncNiadra(KEY, channel="chat", queue=QUIET, http_client=http)
    try:
        await asyncio.sleep(1.0)  # the probe, started with the client: two round trips
        assert niadra.rtt == pytest.approx(0.4, abs=0.05)
        context = await niadra.context(MARINA, conversation_id="c-far", use_cache=False)
    finally:
        await niadra.close(timeout=0)
        await http.aclose()
    assert context.error is None
    assert context.text


async def test_a_set_budget_below_the_round_trip_still_runs_out() -> None:
    region = Region(latency=(0.4, 0.4))
    http = httpx.AsyncClient(transport=httpx.MockTransport(region.handle_async))
    niadra = AsyncNiadra(KEY, channel="chat", queue=QUIET, http_client=http, timeouts=Timeouts(context=0.25))
    try:
        await asyncio.sleep(1.0)
        context = await niadra.context(MARINA, conversation_id="c-ceiling", use_cache=False)
    finally:
        await niadra.close(timeout=0)
        await http.aclose()
    assert context.error == "APITimeoutError"


def test_the_sync_client_measures_when_it_starts() -> None:
    region = Region(latency=(0.2, 0.2))
    http = httpx.Client(transport=httpx.MockTransport(region.handle_sync))
    niadra = Niadra(KEY, channel="chat", queue=QUIET, http_client=http)
    try:
        for _ in range(30):
            if niadra.rtt is not None:
                break
            time.sleep(0.05)
        assert niadra.rtt == pytest.approx(0.2, abs=0.05)
        assert sum(1 for _, path, _ in region.requests if path == "/healthz") == 2
    finally:
        niadra.close(timeout=0)
        http.close()


def test_without_the_probe_the_defaults_apply_as_they_are() -> None:
    c = ClientCore(
        KEY,
        None,
        channel="chat",
        strict=False,
        timeouts=None,
        cache=None,
        queue=None,
        voice=VoiceOptions(probe=False),
    )
    assert c.context_budget("chat", None) == pytest.approx(0.30)
    assert c.navigation_budget(False, None) == pytest.approx(0.60)


def test_a_budget_the_caller_set_never_gets_the_allowance_before_the_probe() -> None:
    assert core(Timeouts(context=0.25)).context_budget("chat", None) == pytest.approx(0.25)


def test_a_probe_that_failed_leaves_the_defaults_as_they_are() -> None:
    """A region the probe cannot reach (a proxy that blocks /healthz) never keeps the allowance for good."""
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"code": "down"}))
    )
    niadra = Niadra(KEY, channel="chat", queue=QUIET, http_client=http)
    try:
        for _ in range(60):
            if not niadra._core.measuring:
                break
            time.sleep(0.05)
        assert niadra._core.measuring is False
        assert niadra.rtt is None
        assert niadra._core.context_budget("chat", None) == pytest.approx(0.30)
    finally:
        niadra.close(timeout=0)
        http.close()
