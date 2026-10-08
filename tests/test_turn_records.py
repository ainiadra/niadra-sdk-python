"""Turn records captured in the agent's process and sent to `POST /v1/turns` (`niadra.turns`)."""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
import respx

from niadra import AsyncNiadra, Niadra, phone
from niadra.models.turns import TurnRecord
from niadra.options import CacheOptions, TurnOptions
from niadra.turns import bind, current_turn, tool
from niadra.turns.capture import TurnFrame
from niadra.turns.digest import digest
from niadra.turns.queue import TurnQueue
from niadra_mock import MOCK_KEY, MockApp
from tests.conftest import BASE, KEY

CUSTOMER = phone("+5511912345678")


@tool("quote")
def quote(plan: str, lives: int) -> dict[str, Any]:
    return {"plan": plan, "lives": lives, "price_full": 511.06}


@tool("stock", provenance=lambda r: [{"ref": f"item_variant:store:{r['sku']}", "fields": {"qty": r["qty"]}}])
async def stock(sku: str) -> dict[str, Any]:
    await asyncio.sleep(0)
    return {"sku": sku, "qty": 3}


@tool()
def broken() -> None:
    raise RuntimeError("the tool failed")


def _records(app: MockApp) -> list[dict[str, Any]]:
    return [stored.record for stored in app.cell.turns.turns.values()]


@pytest.fixture
def niadra(mock_app: MockApp) -> Iterator[Niadra]:
    http = httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi))
    client = Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="whatsapp",
        strict=True,
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=http,
        turns=TurnOptions(interval=3600),
    )
    yield client
    client.close()
    http.close()


def test_a_turn_records_its_read_its_tools_and_what_the_agent_said(mock_app: MockApp, niadra: Niadra) -> None:
    with niadra.conversation("c-1", subject=CUSTOMER, agent_id="closing") as conversation:
        conversation.customer("quanto fica o plano para dois?")
        with conversation.turn(build=niadra.build(prompts={"core": "v16"}, model="provider/model-1")) as turn:
            context = conversation.context()
            quote("regional", 2)
            conversation.agent("Fica R$ 511,06 por mes.")
    assert niadra.flush(5)
    (record,) = _records(mock_app)
    TurnRecord.model_validate(record)
    assert record["turn_id"] == turn.turn_id
    assert record["conversation_id"] == "c-1"
    assert record["agent"] == {"name": "closing"}
    assert record["content_mode"] == "stored"
    assert record["completeness"] == "complete"
    assert [{k: v for k, v in r.items() if k != "blob"} for r in record["reads"]] == [
        {"surface": "pack", "etag": context.etag}
    ]
    assert record["blobs"][record["reads"][0]["blob"]]["content"]["etag"] == context.etag, (
        "the pack it served"
    )
    assert record["build"]["pins"]["prompts"] == {"core": "v16"}
    assert record["build"]["pins"]["niadra"]["pack_hash"]
    assert record["build"]["sdk"].startswith("niadra-python/")
    (call,) = record["calls"]
    assert call["name"] == "quote" and call["call_id"] == "k1" and call["status"] == "ok"
    assert record["blobs"][call["args"]]["content"] == {"plan": "regional", "lives": 2}
    assert record["blobs"][call["result_model"]]["content"]["price_full"] == 511.06
    assert call["args_hash"] == digest({"plan": "regional", "lives": 2})[0]
    assert len(record["output"]["event_keys"]) == 1
    assert record["started_at"] <= record["ended_at"]


def test_capture_copies_on_the_spot(mock_app: MockApp, niadra: Niadra) -> None:
    shared = {"items": [1, 2]}

    @tool("mutating")
    def mutating(query: dict[str, Any]) -> dict[str, Any]:
        return shared

    with niadra.conversation("c-2", subject=CUSTOMER) as conversation, conversation.turn():
        argument = {"q": "shoes"}
        mutating(argument)
        argument["q"] = "changed after the call"
        shared["items"].append(3)
    assert niadra.flush(5)
    (record,) = _records(mock_app)
    (call,) = record["calls"]
    assert record["blobs"][call["args"]]["content"] == {"query": {"q": "shoes"}}
    assert record["blobs"][call["result_model"]]["content"] == {"items": [1, 2]}


async def test_parallel_sub_agents_never_mix_their_turns(mock_app: MockApp, niadra: Niadra) -> None:
    async def sub_agent(name: str, sku: str) -> str:
        with niadra.turns.open(agent=name) as sub:
            await stock(sku)
            await asyncio.sleep(0.01)
            await stock(sku)
            return sub.turn_id

    with (
        niadra.conversation("c-3", subject=CUSTOMER, agent_id="router") as conversation,
        conversation.turn() as outer,
    ):
        first, second = await asyncio.gather(sub_agent("left", "a1"), sub_agent("right", "b2"))
        assert current_turn() is outer
    assert niadra.flush(5)
    by_id = {r["turn_id"]: r for r in _records(mock_app)}
    assert set(by_id) == {outer.turn_id, first, second}
    for turn_id, sku, name in ((first, "a1", "left"), (second, "b2", "right")):
        record = by_id[turn_id]
        assert record["agent"] == {"name": name, "parent_turn_id": outer.turn_id}
        assert record["conversation_id"] == "c-3"
        assert [c["call_id"] for c in record["calls"]] == ["k1", "k2"]
        args = {json.dumps(record["blobs"][c["args"]]["content"]) for c in record["calls"]}
        assert args == {json.dumps({"sku": sku})}
        assert record["calls"][0]["observations"] == [
            {"ref": f"item_variant:store:{sku}", "fields": {"qty": 3}}
        ]
    assert by_id[outer.turn_id]["calls"] == []


def test_a_thread_sees_the_turn_only_when_bound(mock_app: MockApp, niadra: Niadra) -> None:
    with (
        niadra.conversation("c-4", subject=CUSTOMER) as conversation,
        conversation.turn(),
        ThreadPoolExecutor(2) as pool,
    ):
        pool.submit(bind(quote), "bound", 1).result()
        pool.submit(quote, "unbound", 1).result()
    assert niadra.flush(5)
    (record,) = _records(mock_app)
    assert [record["blobs"][c["args"]]["content"]["plan"] for c in record["calls"]] == ["bound"]


def test_a_call_inside_a_tool_names_it_as_its_parent(mock_app: MockApp, niadra: Niadra) -> None:
    @tool("outer")
    def outer() -> dict[str, Any]:
        return quote("inner", 1)

    with niadra.conversation("c-5", subject=CUSTOMER) as conversation, conversation.turn():
        outer()
    assert niadra.flush(5)
    (record,) = _records(mock_app)
    first, second = record["calls"]
    assert (first["name"], second["name"]) == ("outer", "quote")
    assert second["parent_call_id"] == first["call_id"]
    assert "parent_call_id" not in first


def test_a_failing_tool_fails_as_it_would_and_flags_the_turn(mock_app: MockApp, niadra: Niadra) -> None:
    with (
        niadra.conversation("c-6", subject=CUSTOMER) as conversation,
        conversation.turn(),
        pytest.raises(RuntimeError, match="the tool failed"),
    ):
        broken()
    assert niadra.flush(5)
    (record,) = _records(mock_app)
    assert record["calls"][0]["status"] == "error"
    assert record["flags"] == ["error"]


def test_outside_a_turn_a_tool_runs_untouched() -> None:
    assert current_turn() is None
    assert quote("plain", 3) == {"plan": "plain", "lives": 3, "price_full": 511.06}


def test_a_generator_tool_is_recorded_once_consumed(mock_app: MockApp, niadra: Niadra) -> None:
    @tool("pages")
    def pages(n: int) -> Iterator[int]:
        yield from range(n)

    with niadra.conversation("c-7", subject=CUSTOMER) as conversation, conversation.turn():
        assert list(pages(3)) == [0, 1, 2]
        quote("after", 1)
    assert niadra.flush(5)
    (record,) = _records(mock_app)
    generated, after = record["calls"]
    assert record["blobs"][generated["result_model"]]["content"] == [0, 1, 2]
    assert "parent_call_id" not in after


def test_a_value_that_cannot_be_copied_marks_the_turn_incomplete(mock_app: MockApp, niadra: Niadra) -> None:
    @tool("broken_text")
    def broken_text(value: str) -> str:
        return "the tool still answers"

    with niadra.conversation("c-8", subject=CUSTOMER) as conversation, conversation.turn():
        assert broken_text("half of a surrogate pair: \ud800") == "the tool still answers"
    assert niadra.flush(5)
    (record,) = _records(mock_app)
    assert record["completeness"] == "incomplete"
    assert "incomplete" in record["flags"]
    assert "args" not in record["calls"][0]


def test_pointer_mode_keeps_the_values_in_the_company_store(mock_app: MockApp, niadra: Niadra) -> None:
    mock_app.cell.turns.recording_mode = "pointer"
    kept: dict[str, bytes] = {}

    def put(key: str, data: bytes) -> str:
        kept[key] = data
        return f"s3://company-turns/{key}"

    niadra.turns.store(put)
    with niadra.conversation("c-9", subject=CUSTOMER) as conversation, conversation.turn() as turn:
        quote("regional", 2)
    assert niadra.flush(5)
    (record,) = _records(mock_app)
    assert record["content_mode"] == "pointer"
    for key, blob in record["blobs"].items():
        assert "content" not in blob
        path = f"{turn.turn_id}/{key.replace(':', '-')}.json"
        assert blob["pointer"] == f"s3://company-turns/{path}"
        assert digest(json.loads(kept[path]))[0] == blob["sha256"]


def test_a_store_that_fails_sends_digests_only(mock_app: MockApp, niadra: Niadra) -> None:
    def put(key: str, data: bytes) -> str:
        raise OSError("the bucket is down")

    niadra.turns.store(put)
    with niadra.conversation("c-10", subject=CUSTOMER) as conversation, conversation.turn():
        quote("regional", 2)
    assert niadra.flush(5)
    (record,) = _records(mock_app)
    assert record["content_mode"] == "hash_only"
    assert record["completeness"] == "partial"
    assert all(set(blob) == {"sha256", "size"} for blob in record["blobs"].values())


def test_a_source_that_records_less_gets_digests_only(mock_app: MockApp, niadra: Niadra) -> None:
    mock_app.cell.turns.recording_mode = "hash_only"
    for conversation_id in ("c-11", "c-12"):
        with niadra.conversation(conversation_id, subject=CUSTOMER) as conversation, conversation.turn():
            quote("regional", 2)
        assert niadra.flush(5)
    records = _records(mock_app)
    assert [r["content_mode"] for r in records] == ["hash_only", "hash_only"]
    assert niadra.turns.content_mode == "hash_only"
    assert niadra.turns.rejected_turns == 0


def test_a_space_without_turn_records_turns_recording_off(mock_app: MockApp, niadra: Niadra) -> None:
    mock_app.cell.features.discard("turns")
    with niadra.conversation("c-13", subject=CUSTOMER) as conversation, conversation.turn():
        quote("regional", 2)
    assert niadra.flush(5)
    assert niadra.turns.not_kept == 1
    assert not niadra.turns.recording
    with niadra.conversation("c-14", subject=CUSTOMER) as conversation, conversation.turn():
        assert quote("still works", 1)["plan"] == "still works"
    assert niadra.turns.pending == 0


def test_an_outage_keeps_the_turns_for_later(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/turns").mock(
        side_effect=[
            *[httpx.Response(503)] * 3,
            httpx.Response(200, json={"accepted": 1, "duplicates": 0, "errors": []}),
        ]
    )
    client = Niadra(KEY, channel="whatsapp", turns=TurnOptions(interval=3600))
    with client.turns.open(conversation_id="c-15"):
        quote("regional", 2)
    assert not client.flush(5)
    assert client.turns.pending == 1
    client._turn_sender()._pacing.succeeded()
    assert client.flush(5)
    body = json.loads(gzip.decompress(route.calls.last.request.content))
    assert route.calls.last.request.headers["content-encoding"] == "gzip"
    assert [r["conversation_id"] for r in body["turns"]] == ["c-15"]
    client._closed = True


def test_the_same_turn_sent_twice_is_one(mock_app: MockApp, niadra: Niadra) -> None:
    frame = niadra.turns.open(conversation_id="c-16")
    with frame:
        quote("regional", 2)
    niadra.turns.queue.requeue([frame])
    assert niadra.flush(5)
    assert len(_records(mock_app)) == 1
    assert (niadra.turns.accepted, niadra.turns.duplicates) == (1, 1)


def test_a_turn_outside_a_conversation_is_not_recorded(niadra: Niadra) -> None:
    with niadra.turns.open():
        quote("alone", 1)
    assert niadra.turns.pending == 0


async def test_the_async_client_sends_turns_from_its_loop(mock_app: MockApp) -> None:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app.asgi))
    client = AsyncNiadra(
        MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http, turns=TurnOptions(interval=0)
    )
    async with client.conversation("c-17", subject=CUSTOMER) as conversation, conversation.turn():
        await stock("a1")
    for _ in range(100):
        if _records(mock_app):
            break
        await asyncio.sleep(0.01)
    (record,) = _records(mock_app)
    assert record["calls"][0]["name"] == "stock"
    await client.close()
    await http.aclose()


def _frame(size: int, *, flagged: bool = False) -> TurnFrame:
    frame = TurnFrame(None, agent="a", conversation_id="c")
    with frame:
        frame.tool_call("t", {"pad": "x" * size})
    if flagged:
        frame.flag("handoff")
    return frame


def test_a_full_queue_drops_values_before_frames() -> None:
    queue = TurnQueue(max_bytes=40_000, max_turns=100, interval=1.0, batch=50)
    flagged = _frame(10_000, flagged=True)
    plain = [_frame(10_000) for _ in range(3)]
    for frame in (flagged, *plain):
        queue.put(frame)
    assert queue.values_dropped == 1 and queue.turns_dropped == 0
    assert plain[0].completeness == "partial" and not any(b.data for b in plain[0].blobs.values())
    assert flagged.completeness == "complete"
    more = [_frame(10_000) for _ in range(3)]
    for frame in more:
        queue.put(frame)
    assert queue.turns_dropped == 0
    assert len(queue) == 7
    assert flagged.completeness == "complete"
    queue.put(_frame(30_000, flagged=True))
    assert queue.turns_dropped >= 1
    taken = queue.take(50)
    assert flagged not in taken  # the oldest turn went whole once only frames and flagged values were left


def test_a_full_queue_by_count_drops_the_oldest_turns() -> None:
    queue = TurnQueue(max_bytes=10**9, max_turns=2, interval=1.0, batch=50)
    frames = [_frame(10) for _ in range(3)]
    for frame in frames:
        queue.put(frame)
    assert queue.turns_dropped == 1 and queue.values_dropped == 0
    assert queue.take(50) == frames[1:]


def test_putting_a_turn_never_waits_for_the_sender() -> None:
    queue = TurnQueue(max_bytes=10**9, max_turns=10_000, interval=1.0, batch=50)
    blocker = threading.Event()
    done = threading.Event()

    def put_many() -> None:
        for _ in range(1000):
            queue.put(_frame(100))
        done.set()

    thread = threading.Thread(target=put_many)
    thread.start()
    assert done.wait(5)
    blocker.set()
    thread.join()
    assert len(queue) == 1000


def test_the_baggage_processor_puts_the_turn_on_spans(
    mock_app: MockApp, niadra: Niadra, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)  # another module's tests turn the SDK off
    from opentelemetry import baggage
    from opentelemetry import context as otel_context
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from niadra.turns.otel import NiadraBaggageSpanProcessor, turn_baggage

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(NiadraBaggageSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with niadra.conversation("c-18", subject=CUSTOMER) as conversation, conversation.turn() as turn:
        conversation.context()
        with tracer.start_as_current_span("model call"):
            pass
        carried = turn_baggage()
    token = otel_context.attach(baggage.set_baggage("niadra.turn.id", "remote-turn"))
    try:
        with tracer.start_as_current_span("in another process"):
            pass
    finally:
        otel_context.detach(token)
    with tracer.start_as_current_span("outside"):
        pass
    inside, remote, outside = exporter.get_finished_spans()
    assert inside.attributes is not None and inside.attributes["niadra.turn.id"] == turn.turn_id
    assert inside.attributes["niadra.pack.hash"] == turn.pins["niadra"]["pack_hash"]
    assert remote.attributes is not None and remote.attributes["niadra.turn.id"] == "remote-turn"
    assert outside.attributes is not None and "niadra.turn.id" not in outside.attributes
    assert dict(baggage.get_all(carried)) == {
        "niadra.turn.id": turn.turn_id,
        "niadra.pack.hash": turn.pins["niadra"]["pack_hash"],
    }


def test_a_turn_without_a_pin_the_space_requires_is_kept_with_one_warning(
    mock_app: MockApp, niadra: Niadra, caplog: pytest.LogCaptureFixture
) -> None:
    niadra.turns.required_pins = lambda: ("prompts", "model")
    with niadra.conversation("c-19", subject=CUSTOMER) as conversation:
        for _ in range(2):
            with conversation.turn(build=niadra.build(model="model-a")):
                quote("ouro", 2)
        with conversation.turn(build=niadra.build(prompts={"core": "v1"}, model="model-a")):
            quote("ouro", 2)
    assert niadra.flush(5)
    assert len(_records(mock_app)) == 3
    warnings = [r.getMessage() for r in caplog.records if "cannot be replayed" in r.getMessage()]
    assert warnings == [
        "niadra: turns without the prompts pin are kept but cannot be replayed; name them in Niadra.build()"
    ]


def test_a_refused_turn_says_which_field_and_why_never_the_value(
    mock_app: MockApp, niadra: Niadra, caplog: pytest.LogCaptureFixture
) -> None:
    # A whole batch the API refuses: the log carries its detail (field paths and rules) and request id.
    mock_app.cell.fail_next("/v1/turns", 422)
    with caplog.at_level("WARNING", logger="niadra"):
        with niadra.conversation("c-30", subject=CUSTOMER) as conversation, conversation.turn():
            quote("regional", 2)
        assert niadra.flush(5)
    assert niadra.turns.rejected_turns == 1
    assert (
        "1 turn records were refused: HTTP 422 invalid_input: injected by niadra-mock (request "
        in caplog.text
    )


def test_a_refused_write_of_the_outbox_says_which_route_and_why(
    mock_app: MockApp, niadra: Niadra, caplog: pytest.LogCaptureFixture
) -> None:
    mock_app.cell.features.add("coordination")
    mock_app.cell.fail_next("/v1/coordination/declare", 422)
    with caplog.at_level("WARNING", logger="niadra"):
        with niadra.conversation("c-31", subject=CUSTOMER) as conversation:
            conversation.declare.contact_made(None, purpose="transactional", channel="whatsapp")
        assert niadra._outbox.flush(5)
    assert (
        "POST /v1/coordination/declare was refused: HTTP 422 invalid_input: injected by niadra-mock"
        in caplog.text
    )


def test_a_record_cut_to_the_servers_list_sizes_says_it_is_partial() -> None:
    """Every list the record carries is as long as the server takes it; a longer one is never cut silently."""
    from niadra.turns.record import MAX_EVENT_KEYS, MAX_INTERACTIONS, build

    frame = TurnFrame(None, agent="a", conversation_id="c")
    with frame:
        frame.tool_call("t", {"k": 1})
    frame.event_keys.extend(f"k{n}" for n in range(MAX_EVENT_KEYS + 1))
    record = build(frame, "stored")
    assert len(record["output"]["event_keys"]) == MAX_EVENT_KEYS
    assert record["completeness"] == "partial"

    frame = TurnFrame(None, agent="a", conversation_id="c")
    with frame:
        frame.tool_call("t", {"k": 1})
    frame.interactions.extend({"kind": "viewed"} for _ in range(MAX_INTERACTIONS + 1))
    assert build(frame, "stored")["completeness"] == "partial"
