"""The SDK against niadra-mock, in-process: the same code path as production, minus the network."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from niadra import AsyncNiadra, AuthenticationError, Niadra, NotFoundError, email, phone
from niadra_mock import MOCK_KEY, MockApp
from niadra_mock.__main__ import main

MARINA = phone("+5511912345678")
MARINA_MAIL = email("marina@example.com")


def at(minute: int) -> datetime:
    return datetime(2026, 9, 22, 14, minute, tzinfo=timezone.utc)


def say(
    niadra: Niadra,
    text: str,
    *,
    minute: int,
    conversation: str,
    channel: str = "whatsapp",
    role: str = "customer",
    **extra: object,
) -> None:
    niadra.track(
        {
            "channel": channel,
            "conversation_id": conversation,
            "handles": [MARINA],
            "speaker": {"role": role},
            "content": {"text": text},
            "occurred_at": at(minute),
            **extra,
        }
    )


def test_turns_become_a_pinned_pack(on_mock: Niadra) -> None:
    say(on_mock, "I was charged twice", minute=2, conversation="wa-1")
    say(on_mock, "Let me check that for you", minute=3, conversation="wa-1", role="ai_agent")
    on_mock.flush()

    first = on_mock.context(MARINA, conversation_id="voice-1", view="voice")
    assert first.path == "t2"
    assert "whatsapp customer: I was charged twice" in first.system_block
    assert first.system_block.startswith('<context source="niadra"')

    say(on_mock, "Also, the technician never came", minute=5, conversation="wa-1")
    on_mock.flush()
    again = on_mock.context(MARINA, conversation_id="voice-1", view="voice")
    assert again.path == "not_modified", "the SDK revalidated its cached pack by ETag"
    assert again.text == first.text, "pinned: the same bytes on every turn"
    assert [t.text for t in again.live] == ["Also, the technician never came"]

    delta = on_mock.context(MARINA, conversation_id="voice-1", view="voice", delta=True)
    assert delta.delta is not None and "technician" in delta.delta


def test_a_conversation_collects_each_delta_once(on_mock: Niadra) -> None:
    say(on_mock, "I was charged twice", minute=2, conversation="wa-1")
    on_mock.flush()
    with on_mock.conversation("voice-1", subject=MARINA, channel="voice", view="voice") as call:
        pinned = call.context()
        say(on_mock, "The technician never came", minute=5, conversation="wa-1")
        on_mock.flush()
        second = call.context()
        say(on_mock, "Credit issued", minute=6, conversation="billing-1", channel="billing", role="ai_agent")
        on_mock.flush()
        third = call.context()
        fourth = call.context()
    assert second.text == third.text == pinned.text
    assert second.delta is not None and "technician" in second.delta
    assert third.delta is not None
    assert third.delta.count("technician") == 1, "the server sends each change once, the SDK keeps it"
    assert "Credit issued" in third.delta
    assert fourth.delta == third.delta


def test_etag_revalidation(on_mock: Niadra) -> None:
    say(on_mock, "hello", minute=1, conversation="wa-1")
    on_mock.flush()
    first = on_mock.context(MARINA, conversation_id="c-1")
    second = on_mock.context(MARINA, conversation_id="c-1")
    assert second.origin == "cache", "the mock answered not_modified to the known ETag"
    assert second.text == first.text


def test_identify_links_handles_into_one_profile(mock_app: MockApp, on_mock: Niadra) -> None:
    on_mock.track(
        {
            "channel": "email",
            "handles": [MARINA_MAIL],
            "speaker": {"role": "customer"},
            "content": {"text": "Invoice question by e-mail"},
            "occurred_at": at(1),
        }
    )
    on_mock.flush()
    assert "Invoice question" not in on_mock.context(MARINA).system_block
    result = on_mock.identify([MARINA, MARINA_MAIL])
    assert result is not None and result.accepted == 1
    assert mock_app.cell.same_profile(MARINA, MARINA_MAIL)
    assert "Invoice question" in on_mock.context(MARINA).system_block


def test_verification_is_never_inferred(mock_app: MockApp, on_mock: Niadra) -> None:
    say(on_mock, "My card ends in 4242", minute=1, conversation="app-1", verification_hint="V2")
    on_mock.flush()
    before = on_mock.context(MARINA, conversation_id="c-1", verification="V2")
    assert before.verification.effective.value == "V0"
    assert before.verification.reason == "not_proven"
    assert before.withheld == 1 and "4242" not in before.system_block

    on_mock.verify("otp_whatsapp", "V2", handle=MARINA, conversation_id="c-1")
    after = on_mock.context(MARINA, conversation_id="c-1", verification="V2")
    assert after.verification.effective.value == "V2"
    assert "4242" in after.system_block


def test_actions_and_system_events_reach_the_pack(on_mock: Niadra) -> None:
    on_mock.action(
        "credit",
        subject=MARINA,
        object="invoice:erp:0823",
        result="R$ 40",
        channel="billing",
        occurred_at=at(6),
    )
    on_mock.track(
        {
            "kind": "system_event",
            "channel": "erp",
            "canonical_type": "invoice.credited",
            "object_refs": [{"type": "invoice", "namespace": "erp", "id": "0823"}],
            "speaker": {"role": "system"},
            "occurred_at": at(7),
        }
    )
    on_mock.flush()
    pack = on_mock.context(MARINA).system_block
    assert "[Done by agents] 2026-09-22 14:06 billing ai_agent did credit invoice:erp:0823: R$ 40" in pack
    assert "[System] 2026-09-22 14:07 erp invoice.credited invoice:erp:0823" in pack
    by_object = on_mock.context(object="invoice:erp:0823", view="task:billing")
    assert "invoice.credited" in by_object.system_block


def test_search_timeline_and_open(on_mock: Niadra) -> None:
    say(on_mock, "The technician did not show up", minute=1, conversation="call-1", channel="voice")
    say(on_mock, "Technician missed the visit again", minute=2, conversation="wa-2")
    say(on_mock, "What is my balance?", minute=3, conversation="app-3", channel="app")
    on_mock.flush()

    found = on_mock.search(MARINA, "technician visit")
    assert [i.kind for i in found.items] == ["episode", "episode"]
    assert found.recurrence is not None and found.recurrence.occurrences == 2
    assert found.tokens_used > 0
    assert on_mock.search(MARINA, "technician", filters={"channels": ["voice"]}).items[0].channel == "voice"

    page = on_mock.timeline(MARINA, limit=2)
    assert len(page.items) == 2 and page.next_cursor == "2"
    assert page.items[0].at >= page.items[1].at
    rest = on_mock.timeline(MARINA, cursor=page.next_cursor, limit=2)
    assert len(rest.items) == 1 and rest.next_cursor is None

    opened = on_mock.open(found.items[0].id)
    assert opened is not None and opened.kind == "episode"
    assert opened.requested is not None and "echnician" in opened.requested


def test_tools_run_against_the_mock(on_mock: Niadra) -> None:
    say(on_mock, "Refund for order 77 please", minute=1, conversation="wa-1")
    on_mock.flush()
    kit = on_mock.tools(MARINA)
    assert kit.names == ["search_customer_history", "get_customer_timeline", "open_history_item"]
    found = json.loads(kit.call("search_customer_history", {"query": "refund"}))
    opened = json.loads(kit.call("open_history_item", {"id": found["items"][0]["id"]}))
    assert opened["requested"] == "Refund for order 77 please"
    with pytest.raises(NotFoundError):
        kit.call("open_history_item", {"id": "ep_missing"})


def test_a_kit_opens_only_the_bound_customers_items(on_mock: Niadra) -> None:
    say(on_mock, "Refund for order 77 please", minute=1, conversation="wa-1")
    on_mock.flush()
    item = on_mock.search(MARINA, "refund").items[0].id
    assert on_mock.open(item) is not None
    other = on_mock.tools(phone("+5511900000077"))
    with pytest.raises(NotFoundError):
        other.call("open_history_item", {"id": item})


def test_tools_tell_the_model_when_an_item_is_missing(mock_app: MockApp) -> None:
    http = httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi))
    lenient = Niadra(MOCK_KEY, base_url="http://mock", http_client=http)
    assert "unavailable" in lenient.tools(MARINA).call("open_history_item", {"id": "ep_missing"})
    lenient.close()


def test_subject_tokens(on_mock: Niadra) -> None:
    token = on_mock.subject_token(MARINA, conversation_id="c-1", verification="V1")
    assert token is not None and token.token.startswith("st_")
    assert token.expires_at > datetime.now(timezone.utc)


def test_conversations_end_and_duplicates_are_counted(mock_app: MockApp, on_mock: Niadra) -> None:
    with on_mock.conversation("wa-9", subject=MARINA) as conversation:
        conversation.customer("hi", idempotency_key="wamid.1")
        conversation.customer("hi", idempotency_key="wamid.1")
    on_mock.flush()
    assert "wa-9" in mock_app.cell.ended
    assert len(mock_app.cell.events) == 1


def test_bad_items_are_rejected_one_by_one(mock_app: MockApp) -> None:
    http = httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi), base_url="http://mock")
    good = {
        "type": "conversation.ended",
        "idempotency_key": "k1",
        "conversation_id": "c",
        "occurred_at": "2026-09-22T14:00:00Z",
    }
    response = http.post(
        "/v1/batch",
        json={"items": [good, {"type": "event", "channel": "x"}]},
        headers={"Authorization": f"Bearer {MOCK_KEY}"},
    )
    assert response.status_code == 207
    body = response.json()
    assert body["accepted"] == 1 and body["errors"][0]["index"] == 1
    assert http.post("/v1/batch", json={"items": [good]}).status_code == 401
    assert http.get("/v1/nothing", headers={"Authorization": f"Bearer {MOCK_KEY}"}).status_code == 404


def test_batch_response_masked_defaults_empty(mock_app: MockApp) -> None:
    """The emulator does not mask sensitive values, so `masked` comes back empty, never absent."""
    http = httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi), base_url="http://mock")
    good = {
        "type": "conversation.ended",
        "idempotency_key": "k2",
        "conversation_id": "c2",
        "occurred_at": "2026-09-22T14:00:00Z",
    }
    response = http.post("/v1/batch", json={"items": [good]}, headers={"Authorization": f"Bearer {MOCK_KEY}"})
    assert response.status_code == 200
    assert response.json()["masked"] == {}


def test_a_revoked_key_empties_the_cache(mock_app: MockApp, on_mock: Niadra) -> None:
    say(on_mock, "hello", minute=1, conversation="wa-1")
    on_mock.flush()
    assert on_mock.context(MARINA, conversation_id="c-1")
    mock_app.cell.revoke(MOCK_KEY)
    with pytest.raises(AuthenticationError):
        on_mock.context(MARINA, conversation_id="c-1")
    assert len(on_mock._cache) == 0


def test_holdout_profiles_get_an_empty_pack(mock_app: MockApp, on_mock: Niadra) -> None:
    say(on_mock, "hello", minute=1, conversation="wa-1")
    on_mock.flush()
    mock_app.cell.put_in_holdout(MARINA)
    context = on_mock.context(MARINA, conversation_id="c-1")
    assert context.is_holdout and not context and context.error is None


def test_a_421_during_a_cell_move_is_retried(mock_app: MockApp, on_mock: Niadra) -> None:
    mock_app.cell.fail_next("/v1/context", 421)
    assert on_mock.context(MARINA).path == "t2"


def test_an_outage_is_retried_later(mock_app: MockApp, on_mock: Niadra) -> None:
    say(on_mock, "hello", minute=1, conversation="wa-1")
    mock_app.cell.fail_next("/v1/batch", 503, times=3)
    assert not on_mock.flush()
    assert on_mock.pending == 1
    assert on_mock.flush()
    assert len(mock_app.cell.events) == 1


async def test_the_async_client_against_the_mock(mock_app: MockApp, on_mock_async: AsyncNiadra) -> None:
    async with on_mock_async.conversation("wa-1", subject=MARINA) as conversation:
        conversation.customer("Where is my order 77?")
        await on_mock_async.flush()
        context = await conversation.context()
        assert "order 77" in context.system_block
        found = await on_mock_async.search(MARINA, "order")
        assert found.items
        kit = on_mock_async.tools(MARINA)
        assert "order 77" in await kit.call("get_customer_timeline", {})
    await on_mock_async.flush()
    assert "wa-1" in mock_app.cell.ended


def test_the_command_line_parses_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    served: dict[str, object] = {}

    class FakeServer:
        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            served["closed"] = True

    def fake_make_server(host: str, port: int, app: object, **_: object) -> FakeServer:
        served.update(host=host, port=port)
        return FakeServer()

    monkeypatch.setattr("niadra_mock.__main__.make_server", fake_make_server)
    main(["--port", "9999"])
    assert served == {"host": "127.0.0.1", "port": 9999, "closed": True}
