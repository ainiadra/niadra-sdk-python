"""While a conversation is open and in use, the client keeps its connection to the region open (B23): a
turn after a long pause would otherwise pay TCP, TLS and often DNS again, 330 to 650 ms from Sao Paulo."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from niadra import AsyncNiadra, Niadra, VoiceOptions, _async_client, _client, _warm
from niadra._warm import KeepWarm
from tests.conftest import KEY
from tests.test_voice import MARINA, QUIET, Region

NO_PROBE = VoiceOptions(probe=False)


class Session:
    _ended = False


def test_it_pings_only_when_a_conversation_is_open_used_lately_and_the_line_is_quiet() -> None:
    warm = KeepWarm(enabled=True)
    open_one = Session()
    assert warm.add(open_one) is True
    assert warm.add(Session()) is False  # one timer per client
    now = time.monotonic()
    assert warm.step(now, now - 30) == "wait"  # something went out 30 s ago
    assert warm.step(now + 120, now) == "ping"  # quiet for two minutes
    assert warm.step(now + 700, now) == "stop"  # not used for more than ten minutes
    assert warm.add(open_one) is True  # the next conversation starts it again


def test_it_stops_when_every_conversation_ended() -> None:
    warm = KeepWarm(enabled=True)
    session = Session()
    warm.add(session)
    session._ended = True
    now = time.monotonic()
    assert warm.step(now + 120, now) == "stop"


def test_turned_off_it_never_starts() -> None:
    assert KeepWarm(enabled=False).add(Session()) is False


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_client, "KEEP_WARM_EVERY_S", 0.05)
    monkeypatch.setattr(_async_client, "KEEP_WARM_EVERY_S", 0.05)
    monkeypatch.setattr(_warm, "IDLE_S", 0.1)


def pings(region: Region) -> int:
    return sum(1 for _, path, _ in region.requests if path == "/healthz")


@pytest.mark.usefixtures("fast")
def test_the_sync_client_keeps_the_line_warm_while_a_conversation_is_open() -> None:
    region = Region(latency=(0.0, 0.0))
    http = httpx.Client(transport=httpx.MockTransport(region.handle_sync))
    niadra = Niadra(KEY, channel="chat", queue=QUIET, http_client=http, voice=NO_PROBE)
    try:
        conversation = niadra.conversation("c-warm", subject=MARINA)
        time.sleep(0.5)
        assert pings(region) >= 2
        conversation.end()
        time.sleep(0.2)  # the timer sees it ended at its next tick
        after_end = pings(region)
        time.sleep(0.3)
        assert pings(region) == after_end
    finally:
        niadra.close(timeout=0)
        http.close()


@pytest.mark.usefixtures("fast")
def test_keep_warm_false_never_pings() -> None:
    region = Region(latency=(0.0, 0.0))
    http = httpx.Client(transport=httpx.MockTransport(region.handle_sync))
    niadra = Niadra(KEY, channel="chat", queue=QUIET, http_client=http, voice=NO_PROBE, keep_warm=False)
    try:
        conversation = niadra.conversation("c-cold", subject=MARINA)
        time.sleep(0.4)
        assert conversation.id == "c-cold"
        assert pings(region) == 0
    finally:
        niadra.close(timeout=0)
        http.close()


@pytest.mark.usefixtures("fast")
async def test_the_async_client_keeps_the_line_warm_and_its_pings_are_not_its_use() -> None:
    region = Region(latency=(0.0, 0.0))
    http = httpx.AsyncClient(transport=httpx.MockTransport(region.handle_async))
    niadra = AsyncNiadra(KEY, channel="chat", queue=QUIET, http_client=http, voice=NO_PROBE)
    try:
        conversation = niadra.conversation("c-warm-async", subject=MARINA)
        await asyncio.sleep(0.5)
        assert pings(region) >= 2
        assert conversation.id == "c-warm-async"  # held: a conversation nobody holds is gone
        assert niadra._transport.last_activity is None  # the pings are not the client's own use
    finally:
        await niadra.close(timeout=0)
        await http.aclose()
