"""Memory v2 on the SDK side: the customer's turn goes as `query`, the slots land last in the turn
block, the pack stays pinned, a space without memory v2 keeps its old reads, and `prefetch()` never
holds or fails a turn."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx

from niadra import AsyncNiadra, Niadra, phone
from niadra._turns import RECHECK_AFTER, TurnSupport
from niadra.models.results import Context, render_turn
from niadra.options import CacheOptions
from niadra_mock import MOCK_KEY, MockApp
from tests.conftest import BASE, context_payload

MARINA = phone("+5511912345678")


def _say(niadra: Niadra, text: str, *, minute: int, conversation: str, role: str = "customer") -> None:
    niadra.track(
        {
            "channel": "whatsapp",
            "conversation_id": conversation,
            "handles": [MARINA],
            "speaker": {"role": role},
            "content": {"text": text},
            "occurred_at": datetime(2026, 9, 22, 14, minute, tzinfo=timezone.utc),
        }
    )


def _history(niadra: Niadra) -> None:
    _say(niadra, "My protocol number is 81220", minute=1, conversation="email-1")
    for minute in range(2, 12):
        _say(niadra, f"Small talk number {minute}", minute=minute, conversation="wa-old")
    niadra.flush()


def _bodies(app: MockApp) -> list[dict[str, Any]]:
    return app.requests  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def recording(mock_app: MockApp) -> MockApp:
    """The emulator, keeping the body of every `POST /v1/context` it answers."""
    handle = mock_app.handle
    mock_app.requests = []  # type: ignore[attr-defined]

    def keep(
        method: str, path: str, query: str, headers: dict[str, str], body: bytes, scheme: str = "http"
    ) -> Any:
        if path == "/v1/context":
            mock_app.requests.append(json.loads(body))  # type: ignore[attr-defined]
        return handle(method, path, query, headers, body, scheme)

    mock_app.handle = keep  # type: ignore[method-assign]
    return mock_app


def test_the_turn_goes_as_query_and_its_slots_come_last(recording: MockApp, on_mock: Niadra) -> None:
    recording.cell.enable_memory_v2()
    _history(on_mock)
    with on_mock.conversation("voice-1", subject=MARINA, channel="voice", view="voice") as call:
        call.customer("What was the protocol you sent me by email?")
        first = call.context()
        _say(on_mock, "New message on WhatsApp", minute=30, conversation="wa-2")
        on_mock.flush()
        call.customer("And my postal code 04571-010?")
        second = call.context()

    sent = _bodies(recording)
    assert [b.get("query") for b in sent] == [
        "What was the protocol you sent me by email?",
        "And my postal code 04571-010?",
    ]
    assert second.text == first.text, "the pack stays pinned whatever the turn asks"
    assert second.path == "not_modified", "the pinned pack is revalidated by its ETag"
    assert first.slots is not None and "customer: My protocol number is 81220" in first.slots
    assert first.slots.startswith('<turn source="niadra">\nAbout what the customer just said:')
    assert "81220" not in first.text
    assert first.turn_block == first.slots
    assert second.slots is not None and "no record of 04571-010" in second.slots
    live_at, slots_at = second.turn_block.index("<live_turns"), second.turn_block.index("<turn source")
    assert live_at < slots_at < second.turn_block.index("<delta"), "after the live turns, before the delta"


def test_the_slots_are_never_cached(recording: MockApp, mock_app: MockApp) -> None:
    recording.cell.enable_memory_v2()
    http = httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi))
    niadra = Niadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", strict=True, http_client=http)
    _history(niadra)
    with niadra.conversation("wa-9", subject=MARINA) as chat:
        chat.customer("the protocol from the email")
        assert chat.context().slots is not None
        # A fresh cached pack still goes to the API when there is a turn: the slots are this turn's.
        chat.customer("thanks")
        after = chat.context(turn=None)
        assert after.origin == "cache" and after.slots is None
        assert after.turn_block == ""
    assert len(_bodies(recording)) == 1
    niadra.close()
    http.close()


def test_the_pack_as_data_types_the_slots(recording: MockApp, on_mock: Niadra) -> None:
    recording.cell.enable_memory_v2()
    _history(on_mock)
    with on_mock.conversation("wa-3", subject=MARINA) as chat:
        chat.customer("Is order 99123 the one with protocol 81220?")
        first = chat.context(format="json")
        chat.customer("ok")
        second = chat.context(format="json")
    assert first.pack is not None and first.pack.spec == "context-pack.v1"
    derived = [s for s in first.pack.slots if s.section == "derived"]
    assert [(s.derived, s.text) for s in derived] == [
        ("no_record", "[Note] no record of 99123 in this customer's history")
    ]
    items = [s for s in first.pack.slots if s.section != "derived"]
    assert items and items[0].section == "episodes" and items[0].channels == ["lexical"]
    assert first.slots is not None and all(s.text in first.slots for s in first.pack.slots)
    # A read as data asks for the whole answer, so each turn's pack carries its own slots.
    assert "query" in _bodies(recording)[-1] and "known_etag" not in _bodies(recording)[-1]
    assert second.pack is not None and second.pack.slots == [] and second.slots is None


def test_explain_adds_why_to_each_slot(recording: MockApp, on_mock: Niadra) -> None:
    recording.cell.enable_memory_v2()
    _history(on_mock)
    with on_mock.conversation("wa-explain", subject=MARINA) as chat:
        chat.customer("Is order 99123 the one with protocol 81220?")
        answer = chat.context(format="json", explain=True)
    assert answer.pack is not None
    derived = [s for s in answer.pack.slots if s.section == "derived"]
    assert derived and derived[0].why is not None
    assert derived[0].why.rule == "no_record"
    assert derived[0].why.basis.get("identifiers") == 2
    items = [s for s in answer.pack.slots if s.section != "derived"]
    assert items and items[0].why is not None
    why = items[0].why
    assert why.item_id is not None
    assert why.score is not None and why.score > 0
    assert why.channels and why.channels[0].channel == "lexical"
    assert why.channels[0].position == 1
    assert why.channels[0].contribution == why.score
    # `explain` changes nothing else: the bytes are the same as an unexplained read.
    without_explain = _bodies(recording)[-1] | {"explain": False}
    del without_explain["explain"]
    assert "explain" not in without_explain


def test_explain_requires_json_format() -> None:
    strict = Niadra(MOCK_KEY, base_url="http://mock", strict=True)
    with pytest.raises(ValueError, match='explain requires format="json"'):
        strict.context(MARINA, explain=True)
    strict.close()


def test_explain_fails_open_without_strict() -> None:
    lenient = Niadra(MOCK_KEY, base_url="http://mock", channel="whatsapp")
    answer = lenient.context(MARINA, explain=True)
    assert answer.error == "invalid_arguments"
    lenient.close()


def test_a_space_without_memory_v2_keeps_its_pinned_reads(recording: MockApp, on_mock: Niadra) -> None:
    _history(on_mock)
    plain = on_mock.context(MARINA, conversation_id="reference")
    with on_mock.conversation("wa-4", subject=MARINA) as chat:
        chat.customer("the protocol from the email")
        first = chat.context()
        chat.customer("thanks")
        second = chat.context()
    sent = _bodies(recording)[1:]
    assert [b.get("query") for b in sent] == ["the protocol from the email", None, None]
    assert first.text == plain.text and second.text == plain.text, "the pinned pack, as before"
    assert first.slots is None and on_mock._core.turns.reads_turn is False


def test_the_turn_is_asked_again_after_a_while(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    now = [1000.0]
    client._core.turns = TurnSupport(clock=lambda: now[0])
    client.context(MARINA, conversation_id="c-1", turn="first turn")
    client.context(MARINA, conversation_id="c-1", turn="second turn", use_cache=False)
    now[0] += RECHECK_AFTER
    client.context(MARINA, conversation_id="c-1", turn="third turn", use_cache=False)
    queries = [json.loads(call.request.content).get("query") for call in route.calls]
    assert queries == ["first turn", None, None, "third turn", None]


def test_an_old_server_without_slots_keeps_working(respx_mock: respx.MockRouter, client: Niadra) -> None:
    live = [
        {
            "at": "2026-09-22T16:39:00Z",
            "channel": "whatsapp",
            "kind": "message",
            "speaker": "customer",
            "text": "hi",
            "source_id": "s",
        }
    ]
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload(live=live, delta="<delta/>"))
    context = client.context(MARINA, turn="hello")
    assert context.slots is None
    assert context.turn_block == render_turn(context)
    assert context.turn_block.startswith("<live_turns")
    assert context.turn_block.endswith("</live_turns>\n\n<delta/>")


def test_a_failed_turn_read_serves_the_pinned_pack(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    timing = {"total": 3.0, "slots": 1.0}
    route = respx_mock.post(f"{BASE}/v1/context")
    route.side_effect = [
        httpx.Response(200, json=context_payload(timing=timing, slots="<turn>one</turn>")),
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(503),
    ]
    first = lenient.context(MARINA, conversation_id="c-1", turn="one")
    assert first.slots == "<turn>one</turn>"
    second = lenient.context(MARINA, conversation_id="c-1", turn="two")
    assert second.origin == "last_good" and second.text == first.text
    assert second.slots is None, "a pack from the cache never carries an earlier turn's slots"


def test_an_explicit_query_is_a_one_off_read(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    with client.conversation("c-1", subject=MARINA) as chat:
        chat.customer("my turn")
        chat.context(query="invoices")
    assert [json.loads(call.request.content)["query"] for call in route.calls] == ["invoices"]
    assert chat._etag is None, "a focused read leaves the conversation's pin alone"


def test_the_holdout_turn_block_stays_empty() -> None:
    context = Context(path="holdout", slots="<turn/>", delta="<delta/>")
    assert context.turn_block == ""


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Waits for what a background prefetch does, without betting on how long it takes."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


async def test_prefetch_sends_the_partial_turn(mock_app: MockApp, on_mock_async: AsyncNiadra) -> None:
    mock_app.cell.enable_memory_v2()
    async with on_mock_async.conversation("call-1", subject=MARINA, channel="voice", view="voice") as call:
        assert call.prefetch("hm") is False, "too short to say anything"
        assert call.prefetch("what was the protocol") is True
        assert call.prefetch("what was the protocol") is False, "the same text twice"
        assert call.prefetch("what was the protocol you sent") is True
        await until(lambda: len(mock_app.cell.prefetches) == 2 and not on_mock_async._prefetching)
    sent = mock_app.cell.prefetches
    assert [(p.query, p.conversation_id, p.view) for p in sent] == [
        ("what was the protocol", "call-1", "voice"),
        ("what was the protocol you sent", "call-1", "voice"),
    ]
    assert sent[0].subject == MARINA


async def test_prefetch_never_holds_or_fails_a_turn(respx_mock: respx.MockRouter) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(202, json={})

    respx_mock.post(f"{BASE}/v1/context/prefetch").mock(side_effect=slow)
    niadra = AsyncNiadra(
        "nia_sk_live_br1_acme_k1_s3cret", channel="voice", strict=True, cache=CacheOptions(enabled=False)
    )
    conversation = niadra.conversation("call-2", subject=MARINA, view="voice")
    started = time.monotonic()
    assert conversation.prefetch("the internet keeps dropping") is True
    assert conversation.prefetch("the internet keeps dropping again") is True, "it waits its turn"
    assert conversation.prefetch("the internet keeps dropping again at night") is True
    assert time.monotonic() - started < 0.05
    waiting = niadra._prefetching["c:call-2"]
    assert waiting is not None and waiting.query == "the internet keeps dropping again at night"
    await niadra.close(timeout=0)


async def test_a_server_without_prefetch_is_left_alone(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/context/prefetch").respond(404)
    niadra = AsyncNiadra("nia_sk_live_br1_acme_k1_s3cret", channel="voice", strict=True)
    conversation = niadra.conversation("call-3", subject=MARINA, view="voice")
    assert conversation.prefetch("the internet keeps dropping") is True
    await until(lambda: not niadra._prefetching)
    assert conversation.prefetch("the internet keeps dropping at night") is False
    assert route.call_count == 1
    await niadra.close(timeout=0)


def test_prefetch_from_the_sync_client(mock_app: MockApp, on_mock: Niadra) -> None:
    with on_mock.conversation("call-4", subject=MARINA, view="voice") as call:
        assert call.prefetch("the bill came twice this month") is True
    assert on_mock._prefetcher is not None
    on_mock._prefetcher.shutdown(wait=True)
    assert [p.query for p in mock_app.cell.prefetches] == ["the bill came twice this month"]


def test_prefetch_is_skipped_where_the_space_reads_no_turns(on_mock: Niadra) -> None:
    on_mock._core.turns.observe(Context(text="<context/>"))
    with on_mock.conversation("call-5", subject=MARINA, view="voice") as call:
        assert call.prefetch("the bill came twice this month") is False
