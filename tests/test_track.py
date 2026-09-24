from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx

from niadra import EventItem, Niadra, SpeakerRef, email
from niadra._queue import EventBuffer, is_turn
from niadra.models import Content
from niadra.options import QueueOptions
from tests.conftest import BASE, KEY, batch_ok

URL = f"{BASE}/v1/batch"


def message(text: str = "hi", **extra: Any) -> dict[str, Any]:
    return {
        "speaker": {"role": "customer"},
        "handles": [{"type": "email", "value": "m@x.co"}],
        "content": {"text": text},
        **extra,
    }


def sent_items(route: respx.Route) -> list[dict[str, Any]]:
    return [item for call in route.calls for item in json.loads(call.request.content)["items"]]


def wait_for(condition: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


def test_track_only_queues(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok())
    assert client.track(message())
    assert client.pending == 1
    assert not route.called


def test_items_carry_sdk_minted_keys_and_the_default_channel(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok())
    client.track(message())
    client.track(
        EventItem(
            channel="voice",
            handles=[email("m@x.co")],
            speaker=SpeakerRef(role="ai_agent"),
            content=Content(text="hello"),
        )
    )
    client.flush()
    first, second = sent_items(route)
    assert first["channel"] == "whatsapp" and second["channel"] == "voice"
    assert first["idempotency_key"] != second["idempotency_key"]
    assert first["idempotency_key"][14] == "7", "UUIDv7"
    assert "idempotency-key" in route.calls.last.request.headers


def test_a_batch_leaves_when_batch_size_is_reached(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok(3))
    niadra = Niadra(KEY, channel="chat", queue=QueueOptions(batch_size=3, interval=3600))
    try:
        for i in range(3):
            niadra.track(message(f"m{i}"))
        wait_for(lambda: route.called)
        assert len(sent_items(route)) == 3
    finally:
        niadra.close()


def test_a_batch_leaves_after_the_interval(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="chat", queue=QueueOptions(batch_size=100, interval=0.05))
    try:
        niadra.track(message())
        # The route counts the request when it arrives; the queue lets the item go once the answer is back.
        wait_for(lambda: route.called and niadra.pending == 0)
        assert len(sent_items(route)) == 1
    finally:
        niadra.close()


def test_a_conversation_turn_leaves_after_the_turn_interval_not_the_interval(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok(2))
    queue = QueueOptions(batch_size=100, interval=3600, turn_interval=0.05)
    niadra = Niadra(KEY, channel="chat", queue=queue)
    try:
        niadra.track(message("outside any conversation"))
        time.sleep(0.2)
        assert not route.called, "an item outside a conversation waits for the interval"
        # The sender sleeps toward the interval; the turn wakes it for its own, shorter wait.
        niadra.track(message("I was charged twice", conversation_id="wa-1"))
        wait_for(lambda: route.called and niadra.pending == 0)
        assert [item["content"]["text"] for item in sent_items(route)] == [
            "outside any conversation",
            "I was charged twice",
        ]
    finally:
        niadra.close()


def test_only_messages_of_a_conversation_are_turns() -> None:
    turn = message(conversation_id="wa-1", type="event", kind="message")
    assert is_turn(turn)
    assert not is_turn(message())
    assert not is_turn({**turn, "kind": "action"})
    assert not is_turn({**turn, "kind": "system_event"})
    assert not is_turn({"type": "conversation.ended", "conversation_id": "wa-1"})


def test_by_default_a_turn_is_due_well_inside_a_second_and_other_items_in_one() -> None:
    options = QueueOptions()
    assert options.turn_interval == 0.2 and options.interval == 1.0
    other = EventBuffer(options)
    other.put(message())
    assert 0.9 < other.wait_hint() <= 1.0
    turns = EventBuffer(options)
    turns.put(message())
    turns.put(message(conversation_id="wa-1"))
    assert turns.wait_hint() <= 0.2
    turns.take()
    assert turns.next_due() is None


def test_one_request_carries_at_most_500_items(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok())
    for i in range(501):
        client.track(message(f"m{i}"))
    assert client.flush()
    assert [len(json.loads(c.request.content)["items"]) for c in route.calls] == [500, 1]


def test_retryable_failures_go_back_to_the_queue(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    route = respx_mock.post(URL).respond(503)
    lenient.track(message("a"))
    lenient.track(message("b"))
    assert not lenient.flush()
    assert route.call_count == 3, "three attempts, then back to the queue"
    assert lenient.pending == 2
    respx_mock.post(URL).respond(200, json=batch_ok(2))
    assert lenient.flush()
    assert lenient.pending == 0


def test_429_is_retried(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    route = respx_mock.post(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json=batch_ok())]
    )
    lenient.track(message())
    assert lenient.flush()
    assert route.call_count == 2


def test_client_errors_drop_the_batch_without_retry(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    route = respx_mock.post(URL).respond(
        422, json={"title": "invalid", "status": 422, "code": "invalid_input"}
    )
    lenient.track(message())
    assert lenient.flush()
    assert route.call_count == 1
    assert lenient.pending == 0
    assert lenient.dropped == 1


def test_item_errors_in_a_207_are_logged_by_code(
    respx_mock: respx.MockRouter, lenient: Niadra, niadra_logs: pytest.LogCaptureFixture
) -> None:
    respx_mock.post(URL).respond(
        207, json={"accepted": 1, "duplicates": 0, "errors": [{"index": 1, "code": "invalid_item"}]}
    )
    lenient.track(message("secret text"))
    lenient.track(message("more"))
    lenient.flush()
    assert "1 of 2 events rejected (invalid_item)" in niadra_logs.text
    assert "secret text" not in niadra_logs.text


def test_unserializable_events_are_dropped_with_a_log(
    respx_mock: respx.MockRouter, lenient: Niadra, niadra_logs: pytest.LogCaptureFixture
) -> None:
    respx_mock.post(URL).respond(200, json=batch_ok())
    assert not lenient.track(message(fields={"handle": threading.Lock()}))
    assert lenient.pending == 0
    assert "cannot be serialized" in niadra_logs.text


def test_invalid_events_are_dropped_without_echoing_values(
    lenient: Niadra, niadra_logs: pytest.LogCaptureFixture
) -> None:
    assert not lenient.track({"speaker": {"role": "customer"}, "content": {"text": "+5511999998888"}})
    assert "+5511999998888" not in niadra_logs.text
    assert "track failed" in niadra_logs.text


def test_the_queue_is_bounded(respx_mock: respx.MockRouter) -> None:
    niadra = Niadra(KEY, channel="chat", queue=QueueOptions(capacity=2, batch_size=100, interval=3600))
    niadra._closed = True
    assert niadra.track(message("1"))
    assert niadra.track(message("2"))
    assert not niadra.track(message("3"))
    assert (niadra.pending, niadra.dropped) == (2, 1)


def test_close_flushes_what_is_queued(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok())
    with Niadra(KEY, channel="chat", queue=QueueOptions(batch_size=100, interval=3600)) as niadra:
        niadra.track(message())
    assert route.called


def test_a_heartbeat_reports_what_was_sent(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok())
    niadra = Niadra(
        KEY, channel="chat", queue=QueueOptions(batch_size=100, interval=3600, heartbeat_interval=0)
    )
    niadra._closed = True
    niadra.track(message("1"))
    niadra.flush()
    niadra.track(message("2"))
    niadra.flush()
    beats = [i for i in sent_items(route) if i["type"] == "heartbeat"]
    assert beats and beats[0]["sent"] == 1


def test_action_builds_an_action_event(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok())
    occurred = datetime(2026, 9, 22, 14, 6, tzinfo=timezone.utc)
    assert client.action(
        "credit",
        subject=email("m@x.co"),
        object="invoice:erp:0823",
        result="R$ 40 credited",
        closes={"object": "invoice:erp:0823", "operation": "credit"},
        task_id="t-1",
        speaker_id="billing-agent",
        occurred_at=occurred,
        idempotency_key="erp-credit-0823",
    )
    client.flush()
    (item,) = sent_items(route)
    assert item["kind"] == "action"
    assert item["action"]["operation"] == "credit"
    assert item["action"]["closes"] == {
        "object": {"type": "invoice", "namespace": "erp", "id": "0823"},
        "operation": "credit",
    }
    assert item["object_refs"] == [{"type": "invoice", "namespace": "erp", "id": "0823"}]
    assert item["speaker"] == {"role": "ai_agent", "id": "billing-agent"}
    assert item["idempotency_key"] == "erp-credit-0823"
    assert item["occurred_at"] == "2026-09-22T14:06:00Z"


def test_action_closes_by_item_id(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok())
    client.action("reschedule", subject=email("m@x.co"), closes="oi_81")
    client.flush()
    assert sent_items(route)[0]["action"]["closes"] == {"item_id": "oi_81"}


def test_action_refuses_an_ambiguous_closes(lenient: Niadra) -> None:
    assert not lenient.action(
        "credit",
        subject=email("m@x.co"),
        closes={"item_id": "x", "operation": "credit", "object": "invoice:erp:1"},
    )


def test_handoff_is_queued(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(URL).respond(200, json=batch_ok())
    assert client.handoff("c-1", "human", target_source="zendesk", reason="asked for a person", mode="cold")
    client.flush()
    (item,) = sent_items(route)
    assert item["type"] == "handoff"
    assert (item["target"], item["mode"], item["target_source"]) == ("human", "cold", "zendesk")
