"""The agent's working state: compare-and-swap, merge by key, the cap, read your writes, and writes made while
Niadra is out of reach (`niadra.agent_state`)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from niadra import AsyncNiadra, Niadra, phone
from niadra.agent_state import DELETE
from niadra.options import CacheOptions, TurnOptions
from niadra_mock import MOCK_KEY, MockApp

CUSTOMER = phone("+5511912345678")


@pytest.fixture
def app() -> MockApp:
    mock = MockApp()
    mock.cell.features.add("agent_state")
    return mock


@pytest.fixture
def niadra(app: MockApp) -> Iterator[Niadra]:
    client = Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="whatsapp",
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=httpx.Client(transport=httpx.WSGITransport(app=app.wsgi)),
        turns=TurnOptions(interval=3600),
    )
    yield client
    client.close()


def test_compare_and_swap_creates_then_conflicts_at_an_old_version(niadra: Niadra) -> None:
    with niadra.conversation("c-1", subject=CUSTOMER, agent_id="closing") as conversation:
        state = conversation.agent_state
        assert state.get().version == 0
        created = state.put({"step": "quote"}, mode="cas", if_version=0)
        assert (created.stored, created.version) == (True, 1)
        stale = state.put({"step": "close"}, mode="cas", if_version=0)
        assert (stale.stored, stale.reason) == (False, "conflict")
        assert state.get().body == {"step": "quote"}


def test_merge_by_key_keeps_the_other_fields_and_a_removed_one_stays_gone(niadra: Niadra) -> None:
    with niadra.conversation("c-2", subject=CUSTOMER, agent_id="closing") as conversation:
        state = conversation.agent_state
        state.put({"cart": ["sku-1"], "offer": {"status": "shown"}})
        state.put({"offer": {"status": "accepted"}})
        state.put({"cart": DELETE})
        read = state.get()
    assert (read.version, read.body) == (3, {"offer": {"status": "accepted"}})


def test_a_large_state_is_kept_whole(niadra: Niadra) -> None:
    with niadra.conversation("c-3", subject=CUSTOMER, agent_id="closing") as conversation:
        conversation.agent_state.put({"note": "short"})
        big = conversation.agent_state.put({"note": "x" * 20_000})
        assert (big.stored, big.reason, big.version) == (True, None, 2)
        assert conversation.agent_state.get().body == {"note": "x" * 20_000}


def test_a_read_never_goes_back_behind_a_write(app: MockApp, niadra: Niadra) -> None:
    with niadra.conversation("c-4", subject=CUSTOMER, agent_id="closing") as conversation:
        conversation.agent_state.put({"step": "quote"})
        conversation.agent_state.put({"step": "close"})
        app.cell.state.agent_states[("conversation", "c-4", "closing")] = (
            {"step": "quote"},
            1,
            app.cell.clock(),
        )
        assert conversation.agent_state.get().version == 2  # a stale copy never wins over what we wrote


def test_with_niadra_down_the_state_is_kept_and_sent_again(app: MockApp, niadra: Niadra) -> None:
    with niadra.conversation("c-5", subject=CUSTOMER, agent_id="closing") as conversation:
        state = conversation.agent_state
        state.put({"step": "quote"}, mode="cas", if_version=0)
        app.cell.fail_next("/", 503, times=1000)
        later = state.put({"step": "close"}, mode="cas", if_version=1)
        assert (later.stored, later.pending, later.version) == (True, True, 2)
        read = state.get()
        assert (read.degraded, read.body, read.version) == (True, {"step": "close"}, 2)
        app.cell.failures.clear()
        niadra._outbox._pacing.succeeded()
        assert niadra.flush(5)
        assert state.get().body == {"step": "close"} and not state.conflicts


def test_a_write_sent_again_that_meets_a_newer_version_is_reported(app: MockApp, niadra: Niadra) -> None:
    with niadra.conversation("c-6", subject=CUSTOMER, agent_id="closing") as conversation:
        state = conversation.agent_state
        state.put({"step": "quote"}, mode="cas", if_version=0)
        app.cell.fail_next("/", 503, times=1000)
        assert state.put({"step": "close"}, mode="cas", if_version=1).pending
        app.cell.failures.clear()
        # Another process wrote meanwhile.
        app.cell.state.agent_states[("conversation", "c-6", "closing")] = (
            {"step": "lost"},
            2,
            app.cell.clock(),
        )
        niadra._outbox._pacing.succeeded()
        assert niadra.flush(5)
        (conflict,) = state.conflicts
        assert (conflict.body, conflict.if_version) == ({"step": "close"}, 1)
        assert state.get().body == {"step": "lost"}


async def test_the_async_client_reads_its_writes(app: MockApp) -> None:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.asgi))
    client = AsyncNiadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http)
    async with client.conversation("c-7", subject=CUSTOMER, agent_id="closing") as conversation:
        await conversation.agent_state.put({"step": "quote"})
        read = await conversation.agent_state.get()
    assert (read.version, read.body) == (1, {"step": "quote"})
    await client.close(timeout=1)
    await http.aclose()
