from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx

from niadra import HistoryFilters, Niadra, Verification, current_session, email, phone
from niadra.options import QueueOptions, VoiceOptions
from niadra.tools import BUILTIN_DEFINITIONS
from tests.conftest import BASE, KEY, batch_ok, context_payload

MARINA = phone("+5511912345678")

SEARCH = {
    "items": [
        {
            "id": "ep_1",
            "kind": "episode",
            "text": "technician missed the visit",
            "at": "2026-09-22T14:02:00Z",
            "channel": "voice",
            "outcome": "rescheduled",
        }
    ],
    "recurrence": {"category": "technical_visit", "occurrences": 3, "window_days": 90},
    "withheld": 1,
    "tokens_used": 12,
}


def body(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


def test_search(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/history/search").respond(200, json=SEARCH)
    result = client.search(
        MARINA,
        "technician",
        filters={"channels": ["voice"]},
        max_tokens=300,
        verification="V1",
        conversation_id="c-1",
    )
    assert body(route) == {
        "subject": {"type": "phone_e164", "value": "+5511912345678"},
        "query": "technician",
        "filters": {"channels": ["voice"], "categories": [], "item_kinds": [], "item_statuses": []},
        "max_tokens": 300,
        "verification": "V1",
        "conversation_id": "c-1",
    }
    assert result.items[0].outcome == "rescheduled"
    assert result.recurrence is not None and result.recurrence.occurrences == 3
    assert (result.withheld, result.error) == (1, None)


def test_timeline_lists_resolved_items_with_what_closed_them(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    resolved = {
        "id": "019a7c2e-5b1d-7f00-8000-0000000000a1",
        "kind": "open_item",
        "text": "Credit invoice 0823 · kept on 22/09",
        "at": "2026-09-22T14:02:00Z",
        "outcome": "resolved",
        "object": {"type": "invoice", "namespace": "erp", "id": "0823"},
        "expected_operation": "credit",
        "status": "resolved",
        "closed_at": "2026-09-22T14:06:00Z",
        "closed_by": {"kind": "action", "action_id": "a-1", "event_id": "e-1", "operation": "credit"},
    }
    route = respx_mock.post(f"{BASE}/v1/history/timeline").respond(
        200, json={"items": [resolved], "next_cursor": None}
    )
    page = client.timeline(MARINA, filters={"item_kinds": ["open_item"], "item_statuses": ["resolved"]})
    assert body(route)["filters"]["item_statuses"] == ["resolved"]
    [done] = page.items
    assert done.id == "019a7c2e-5b1d-7f00-8000-0000000000a1"
    assert (done.status, done.expected_operation) == ("resolved", "credit")
    assert done.object is not None and done.object.id == "0823"
    assert done.closed_at == datetime(2026, 9, 22, 14, 6, tzinfo=timezone.utc)
    assert done.closed_by is not None
    assert (done.closed_by.kind, done.closed_by.action_id, done.closed_by.operation) == (
        "action",
        "a-1",
        "credit",
    )
    for status in ("closed", "merged"):
        with pytest.raises(ValueError, match="item_statuses"):
            HistoryFilters.model_validate({"item_statuses": [status]})


def test_open_sends_the_bare_id_the_row_listed(respx_mock: respx.MockRouter, client: Niadra) -> None:
    episode = "019a7c2e-5b1d-7f00-8000-0000000000e1"
    route = respx_mock.post(f"{BASE}/v1/history/open").respond(
        200, json={"id": episode, "kind": "episode", "summary": "Asked to move the visit"}
    )
    item = client.open(episode, verification="V2")
    assert body(route)["item_id"] == episode
    assert item is not None and (item.id, item.kind) == (episode, "episode")


def test_navigation_budgets(respx_mock: respx.MockRouter, quiet_queue: QueueOptions) -> None:
    budgets: list[float] = []

    def record(request: httpx.Request) -> httpx.Response:
        budgets.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=SEARCH)

    respx_mock.post(f"{BASE}/v1/history/search").mock(side_effect=record)
    # No probe in flight beside the reads: it would hold the one open connection, and a read would open one.
    client = Niadra(KEY, channel="whatsapp", queue=quiet_queue, strict=True, voice=VoiceOptions(probe=False))
    client._transport._warmth.answered()  # a connection is open and idle: the budgets are exact
    client._core.rtt = 0.0  # measured, in the region: the defaults apply as they are
    client.search(MARINA, "x")
    client.search(MARINA, "x", voice=True)
    assert budgets == [pytest.approx(0.6, abs=0.01), pytest.approx(0.3, abs=0.01)]
    client._closed = True


def test_timeline_keeps_the_handle_out_of_the_url(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/history/timeline").respond(
        200, json={"items": SEARCH["items"], "next_cursor": "20"}
    )
    page = client.timeline(MARINA, cursor="0", limit=20)
    assert "5511912345678" not in str(route.calls.last.request.url)
    assert body(route)["cursor"] == "0"
    assert page.next_cursor == "20"


def test_open_keeps_the_conversation_id_out_of_the_url(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/history/open").respond(
        200,
        json={
            "id": "ep_1",
            "kind": "episode",
            "summary": "missed visit",
            "promises": [{"by": "company", "what": "visit on 23/09", "status": "open"}],
        },
    )
    # A conversation id may be a phone number: it travels in the body of POST /v1/history/open.
    item = client.open("ep_1", verification="V2", conversation_id="+5511912345678")
    assert item is not None and item.promises[0].by == "company"
    sent = route.calls.last.request
    assert str(sent.url) == f"{BASE}/v1/history/open"
    assert body(route) == {"item_id": "ep_1", "verification": "V2", "conversation_id": "+5511912345678"}
    client.open("ep_1", subject=MARINA)
    # The customer goes along when given.
    assert body(route) == {
        "item_id": "ep_1",
        "subject": MARINA.model_dump(mode="json", exclude_none=True),
        "verification": "V0",
    }


def test_open_sends_an_id_with_slashes_in_the_body_and_refuses_an_empty_one(
    respx_mock: respx.MockRouter, lenient: Niadra
) -> None:
    route = respx_mock.post(f"{BASE}/v1/history/open").respond(404, json={"code": "not_found"})
    assert lenient.open("../profiles/1") is None
    assert str(route.calls.last.request.url) == f"{BASE}/v1/history/open"
    assert body(route)["item_id"] == "../profiles/1"
    assert lenient.open("") is None
    assert route.call_count == 1


def test_identify_is_sent_at_once(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    result = client.identify([MARINA, email("marina@example.com")], method="otp", conversation_id="c-1")
    assert result is not None and result.accepted == 1
    (item,) = body(route)["items"]
    assert (item["type"], item["method"], len(item["handles"])) == ("identify", "otp", 2)


def test_identify_needs_two_handles(lenient: Niadra) -> None:
    assert lenient.identify([MARINA]) is None


def test_verify_drops_the_cached_pack_of_that_conversation(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    context = respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    client.context(MARINA, conversation_id="c-1", verification="V2")
    client.context(MARINA, conversation_id="c-2", verification="V2")
    client.verify("otp_whatsapp", "V2", handle=MARINA, conversation_id="c-1")
    (item,) = body(batch)["items"]
    assert (item["type"], item["method"], item["level"]) == ("verify", "otp_whatsapp", "V2")
    client.context(MARINA, conversation_id="c-1", verification="V2")
    client.context(MARINA, conversation_id="c-2", verification="V2")
    assert context.call_count == 3


def test_a_failed_verify_is_queued_for_retry(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    respx_mock.post(f"{BASE}/v1/batch").respond(503)
    assert lenient.verify("login", "V1", handle=MARINA, conversation_id="c-1") is None
    assert lenient.pending == 1


def test_a_rejected_verify_is_not_queued(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    respx_mock.post(f"{BASE}/v1/batch").respond(
        422, json={"title": "x", "status": 422, "code": "invalid_input"}
    )
    assert lenient.verify("login", "V1", handle=MARINA, conversation_id="c-1") is None
    assert lenient.pending == 0


def test_subject_token(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/subject-tokens").respond(
        200, json={"token": "st_abc", "expires_at": "2026-09-22T14:22:00Z"}
    )
    token = client.subject_token(MARINA, conversation_id="c-1", verification="V1")
    assert token is not None and token.headers == {"Niadra-Subject-Token": "st_abc"}
    assert body(route) == {
        "subject": {"type": "phone_e164", "value": "+5511912345678"},
        "conversation_id": "c-1",
        "verification": "V1",
    }
    assert "idempotency-key" in route.calls.last.request.headers
    assert client.mcp_url == f"{BASE}/mcp"


def test_tools_bind_the_customer_outside_the_model(respx_mock: respx.MockRouter, client: Niadra) -> None:
    search = respx_mock.post(f"{BASE}/v1/history/search").respond(200, json=SEARCH)
    kit = client.tools(MARINA, conversation_id="c-1", verification="V1", voice=True)
    assert kit.names == ["search_customer_history", "get_customer_timeline", "open_history_item"]
    assert kit.definitions == BUILTIN_DEFINITIONS
    assert all("subject" not in json.dumps(d["function"]["parameters"]) for d in kit.definitions)
    assert [t["name"] for t in kit.anthropic_definitions()] == kit.names

    output = kit.call(
        "search_customer_history", '{"query": "technician", "filters": {"outcome": "resolved"}}'
    )
    sent = body(search)
    assert sent["subject"]["value"] == "+5511912345678"
    assert sent["filters"]["outcome"] == "resolved"
    assert (sent["max_tokens"], sent["verification"], sent["conversation_id"]) == (300, "V1", "c-1")
    assert json.loads(output)["items"][0]["id"] == "ep_1"

    opened = respx_mock.post(f"{BASE}/v1/history/open").respond(
        200, json={"id": "ep_1", "kind": "episode", "summary": "missed visit"}
    )
    assert json.loads(kit.call("open_history_item", {"id": "ep_1"}))["kind"] == "episode"
    # The bound customer goes in the body, so the server opens only an item of theirs.
    assert body(opened) == {
        "item_id": "ep_1",
        "subject": {"type": "phone_e164", "value": "+5511912345678"},
        "verification": "V1",
        "conversation_id": "c-1",
    }
    assert "c-1" not in str(opened.calls.last.request.url)


def test_tools_make_no_request_until_the_model_calls_one(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    client.tools(MARINA)
    assert not respx_mock.calls


def test_tool_calls_never_raise_at_the_model(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    respx_mock.post(f"{BASE}/v1/history/search").respond(500)
    kit = lenient.tools(MARINA)
    assert "unavailable" in kit.call("search_customer_history", {"query": "x"})
    assert "query is required" in kit.call("search_customer_history", {})
    assert "invalid arguments" in kit.call("search_customer_history", "{not json")
    assert "invalid arguments" in kit.call("get_customer_timeline", {"filters": {"since": "yesterday"}})
    assert "unknown tool" in kit.call("drop_tables", {})
    assert "id is required" in kit.call("open_history_item", None)


def test_conversation_pins_captures_and_ends(respx_mock: respx.MockRouter, client: Niadra) -> None:
    context = respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    with client.conversation("c-1", subject=MARINA, view="chat", agent_id="bot-7") as conversation:
        assert current_session() is conversation
        conversation.customer("I was charged twice")
        conversation.context()
        conversation.context()
        conversation.mark_injected()
        conversation.agent("I see a credit of R$ 40 was issued yesterday.")
        conversation.action("refund", object="invoice:erp:0823")
        conversation.action("note", object="invoice:erp:0823", speaker="human_agent")
        conversation.handoff("human", reason="wants a person")
    assert current_session() is None
    # Each read sent the customer's turn, which picks that turn's slots: a read with a turn always goes
    # to the API, and the pack it keeps is the conversation's pinned one.
    assert context.call_count == 2
    first, second = (json.loads(call.request.content) for call in context.calls)
    assert first["query"] == second["query"] == "I was charged twice"
    assert body(context)["conversation_id"] == "c-1"
    client.flush()
    items = [i for call in batch.calls for i in json.loads(call.request.content)["items"]]
    kinds = [(i["type"], i.get("kind"), i.get("speaker", {}).get("role")) for i in items]
    assert kinds == [
        ("event", "message", "customer"),
        ("event", "message", "ai_agent"),
        ("event", "action", "ai_agent"),
        ("event", "action", "human_agent"),
        ("handoff", None, None),
        ("conversation.ended", None, None),
    ]
    customer, agent, action, human_action = items[0], items[1], items[2], items[3]
    assert customer["direction"] == "inbound" and "id" not in customer["speaker"]
    assert "context_stamp" not in customer
    assert agent["speaker"]["id"] == "bot-7"
    assert "fields" not in agent or not agent["fields"]
    assert agent["context_stamp"]["etag"] == "etag-1"
    assert agent["context_stamp"]["injected_at"] <= agent["occurred_at"]
    assert action["context_stamp"] == agent["context_stamp"]
    assert "context_stamp" not in human_action
    assert action["conversation_id"] == "c-1" and action["handles"][0]["value"] == "+5511912345678"
    assert conversation.context_injected_at is not None
    assert conversation.first_agent_turn_at is not None


def test_conversation_ends_even_when_the_block_raises(respx_mock: respx.MockRouter, client: Niadra) -> None:
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    with pytest.raises(RuntimeError), client.conversation("c-9", subject=MARINA):
        raise RuntimeError("agent crashed")
    client.flush()
    assert body(batch)["items"][-1] == {
        **body(batch)["items"][-1],
        "type": "conversation.ended",
        "conversation_id": "c-9",
    }


def test_a_task_centers_on_its_object_and_emits_task_ended(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    context = respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    with client.task("t-1", subject=MARINA, object="invoice:erp:0823", view="task:billing") as task:
        task.context()
        task.action("credit", result="R$ 40")
    sent = body(context)
    assert "subject" not in sent and sent["object"]["id"] == "0823" and sent["task_id"] == "t-1"
    client.flush()
    items = [i for call in batch.calls for i in json.loads(call.request.content)["items"]]
    assert items[0]["task_id"] == "t-1" and items[0]["kind"] == "action"
    assert items[-1] == {**items[-1], "type": "task.ended", "task_id": "t-1"}


def test_agent_turns_before_any_injection_carry_no_stamp(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    with client.conversation("c-1", subject=MARINA) as conversation:
        conversation.agent("Hello, how can I help?")
        assert conversation.first_agent_turn_at is not None
        assert conversation.context_injected_at is None
    client.flush()
    assert "context_stamp" not in body(batch)["items"][0]


def test_each_injection_restamps_the_following_turns(respx_mock: respx.MockRouter, client: Niadra) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    first_at = datetime(2026, 9, 22, 14, 7, 2, tzinfo=timezone.utc)
    later_at = datetime(2026, 9, 22, 14, 8, 30, tzinfo=timezone.utc)
    with client.conversation("c-1", subject=MARINA) as conversation:
        conversation.mark_injected(at=first_at)  # nothing fetched yet: no etag to name
        conversation.agent("One moment.")
        conversation.mark_injected(conversation.context(), at=later_at)
        conversation.agent("I see the credit.")
    assert conversation.context_injected_at == first_at
    client.flush()
    stamps = [i["context_stamp"] for i in body(batch)["items"] if i.get("kind") == "message"]
    assert stamps == [
        {"injected_at": "2026-09-22T14:07:02Z"},
        {"etag": "etag-1", "injected_at": "2026-09-22T14:08:30Z"},
    ]


def test_a_conversation_keeps_every_delta_until_the_pack_changes(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    live = [
        {
            "at": "2026-09-22T14:07:02Z",
            "channel": "voice",
            "kind": "message",
            "speaker": "customer",
            "text": "called",
            "source_id": "s",
        }
    ]
    route = respx_mock.post(f"{BASE}/v1/context").mock(
        side_effect=[
            httpx.Response(200, json=context_payload()),
            httpx.Response(200, json=context_payload(delta="[New] credit of R$ 40", live=live)),
            httpx.Response(200, json=context_payload(delta="[New] visit rescheduled")),
            httpx.Response(200, json=context_payload(delta="[New] visit rescheduled")),
            httpx.Response(200, json=context_payload(etag="etag-2", text="<niadra>V2</niadra>", delta="x")),
        ]
    )
    with client.conversation("c-1", subject=MARINA) as conversation:
        first = conversation.context(use_cache=False)
        second = conversation.context(use_cache=False)
        third = conversation.context(use_cache=False)
        fourth = conversation.context(use_cache=False)
        repinned = conversation.context(use_cache=False)
    sent = [json.loads(call.request.content) for call in route.calls]
    assert "delta" not in sent[0] or sent[0]["delta"] is False
    assert all(request["delta"] is True for request in sent[1:])
    assert first.turn_block == ""
    assert second.text == first.text
    assert second.turn_block.startswith("<live_turns")
    assert second.turn_block.endswith("</live_turns>\n\n[New] credit of R$ 40")
    assert third.turn_block == "[New] credit of R$ 40\n\n[New] visit rescheduled"
    assert fourth.turn_block == third.turn_block, "a repeated delta is kept once"
    assert (repinned.text, repinned.turn_block) == ("<niadra>V2</niadra>", "")


def test_an_empty_answer_drops_the_deltas(respx_mock: respx.MockRouter, client: Niadra) -> None:
    respx_mock.post(f"{BASE}/v1/context").mock(
        side_effect=[
            httpx.Response(200, json=context_payload()),
            httpx.Response(200, json=context_payload(delta="[New] refund")),
            httpx.Response(403),
            httpx.Response(200, json=context_payload()),
        ]
    )
    lenient = Niadra(KEY, channel="whatsapp")
    lenient._closed = True
    with lenient.conversation("c-1", subject=MARINA) as conversation:
        conversation.context(use_cache=False)
        assert conversation.context(use_cache=False).turn_block == "[New] refund"
        cut = conversation.context(use_cache=False)
        back = conversation.context(use_cache=False)
    assert (cut.origin, cut.text, cut.turn_block) == ("empty", None, "")
    assert back.turn_block == ""


def test_session_verify_raises_the_level_and_starts_from_the_new_pack(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    context = respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    with client.conversation("c-1", subject=MARINA) as conversation:
        conversation.context()
        assert conversation.verify("otp_whatsapp", "V2") is not None
        conversation.context()
    assert conversation.verification is Verification.V2
    sent = json.loads(batch.calls[0].request.content)["items"][0]
    assert sent == {**sent, "type": "verify", "level": "V2", "conversation_id": "c-1"}
    assert sent["handle"]["value"] == "+5511912345678"
    last = json.loads(context.calls.last.request.content)
    assert last["verification"] == "V2"
    assert last.get("delta") is not True


def test_session_tools_bind_the_session_and_follow_its_level(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    search = respx_mock.post(f"{BASE}/v1/history/search").respond(200, json=SEARCH)
    respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    account = {"type": "system_id", "scope": "crm", "value": "A-9"}
    with client.conversation("call-1", subject=MARINA, about=account, view="voice") as conversation:
        kit = conversation.tools()
        assert kit is not None and kit.voice
        conversation.verify("otp_sms", "V2")
        kit.call("search_customer_history", {"query": "technician"})
    sent = json.loads(search.calls.last.request.content)
    assert sent["verification"] == "V2" and sent["conversation_id"] == "call-1"
    assert sent["about"]["value"] == "A-9"
    assert search.calls.last.request.extensions["timeout"]["read"] <= 0.3
    # Open reaches what the pack's account block shows too: the organization goes along.
    opened = respx_mock.post(f"{BASE}/v1/history/open").respond(
        200, json={"id": "ep_9", "kind": "episode", "summary": "renewal"}
    )
    kit.call("open_history_item", {"id": "ep_9"})
    assert json.loads(opened.calls.last.request.content)["about"]["value"] == "A-9"


def test_an_unlinked_about_is_said_once_per_session(
    respx_mock: respx.MockRouter, client: Niadra, caplog: pytest.LogCaptureFixture
) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json={**context_payload(), "about_unlinked": True})
    respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    account = {"type": "system_id", "scope": "crm", "value": "A-9"}
    with (
        caplog.at_level("WARNING", logger="niadra"),
        client.conversation("call-2", subject=MARINA, about=account) as conversation,
    ):
        first = conversation.context()
        conversation.context(delta=True)
    # The read went on with the subject's own memory, and the flag says so.
    assert first.about_unlinked and first.text
    assert sum("no active link" in r.getMessage() for r in caplog.records) == 1


def test_session_tools_need_a_subject(client: Niadra) -> None:
    with client.task("t-1", object="invoice:erp:0823") as task:
        assert task.tools() is None
    with client.task("t-2", subject=MARINA) as task:
        kit = task.tools()
        assert kit is not None and (kit.task_id, kit.conversation_id) == ("t-2", None)
