"""The agent's own memory, the canonical tool definitions and the new read features, against the
emulator."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import respx

from niadra import AsyncNiadra, Niadra, UnprocessableEntityError, phone
from niadra.tools import ALL_DEFINITIONS, BUILTIN_DEFINITIONS, MEMORY_UNAVAILABLE, PERSONAL_DATA, definitions
from niadra_mock import MockApp
from tests.conftest import BASE

FIXTURE = Path(__file__).parent / "fixtures" / "tool-definitions.json"
MARINA = phone("+5511912345678")
PROCEDURE = "Post the credit, then refresh the invoice; the credit shows only after both."


def test_the_definitions_are_the_apis_byte_for_byte() -> None:
    shipped = json.dumps(ALL_DEFINITIONS, indent=2, ensure_ascii=False) + "\n"
    assert shipped.encode() == FIXTURE.read_bytes()
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert digest == "dd04e8f1a8a2031d0b286a481df9ba89fec937ffaf16d3d2e0230111dc61f653", (
        "same file as the TS SDK"
    )
    names = [d["function"]["name"] for d in ALL_DEFINITIONS]
    assert names == [
        "search_customer_history",
        "get_customer_timeline",
        "open_history_item",
        "search_agent_memory",
        "remember",
    ]
    assert definitions() == BUILTIN_DEFINITIONS
    assert [d["function"]["name"] for d in definitions(agent_memory=True)][-1] == "search_agent_memory"
    assert definitions(agent_memory=True, write_agent_memory=True) == ALL_DEFINITIONS
    for definition in ALL_DEFINITIONS:
        assert "subject" not in json.dumps(definition["function"]["parameters"])


def test_the_emulator_lists_the_same_tools(on_mock: Niadra, mock_app: MockApp) -> None:
    response = on_mock._transport.request(
        __import__("niadra._transport", fromlist=["Request"]).Request(
            "GET", "/v1/history/tools", params={"agent_memory": "true"}
        )
    )
    assert response == ALL_DEFINITIONS
    mock_app.cell.agent_memory_writers.clear()
    shorter = on_mock._transport.request(
        __import__("niadra._transport", fromlist=["Request"]).Request(
            "GET", "/v1/history/tools", params={"agent_memory": "true"}
        )
    )
    assert shorter == definitions(agent_memory=True)


def test_remember_then_read_the_block_and_search(on_mock: Niadra, mock_app: MockApp) -> None:
    empty = on_mock.agent_memory()
    assert empty.enabled and empty.text == "" and empty.source == "network"
    saved = on_mock.remember(
        "procedure", "Credit on an invoice", PROCEDURE, tags=["invoice", "credit", "erp"]
    )
    assert saved and saved.note is not None and saved.note.version == 1
    block = on_mock.agent_memory(tags=["invoice"])
    assert PROCEDURE in block.text and block.notes == [saved.note.note_id] and block.tokens > 0
    found = on_mock.search_agent_memory("how to credit an invoice", tags=["credit"])
    assert [n.note_id for n in found] == [saved.note.note_id]
    assert on_mock.search_agent_memory("reschedule a visit") == []


def test_the_block_is_cached_and_revalidated_by_etag(mock_app: MockApp) -> None:
    import httpx

    from niadra.options import CacheOptions

    http = httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi))
    niadra = Niadra(
        "nia_sk_test_local_mock_k1_mocksecret",
        base_url="http://mock",
        strict=True,
        cache=CacheOptions(ttl=0),
        http_client=http,
    )
    niadra.remember(
        "pitfall", "Scheduling API", "Dates without a time zone are refused.", tags=["scheduling"]
    )
    first = niadra.agent_memory()
    again = niadra.agent_memory()
    assert (first.source, again.source) == ("network", "cache"), "the second read got a 304"
    assert again.text == first.text
    niadra.close()
    http.close()


def test_a_note_with_personal_data_is_refused(on_mock: Niadra, lenient: Niadra, mock_app: MockApp) -> None:
    with pytest.raises(UnprocessableEntityError) as refused:
        on_mock.remember("procedure", "Call back", "Call +55 11 91234-5678 after the credit.")
    assert refused.value.code == "personal_data_in_agent_memory"
    on_mock.track(
        {
            "conversation_id": "c-1",
            "handles": [MARINA],
            "speaker": {"role": "customer"},
            "content": {"text": "hi"},
        }
    )
    on_mock.flush()
    kit = on_mock.tools(MARINA, conversation_id="c-1", agent_memory=True, write_agent_memory=True)
    body = "Customer 5511912345678 prefers mornings."
    answer = kit.call("remember", {"kind": "tool_note", "title": "Visits", "body": body})
    assert answer == PERSONAL_DATA, "the exact error, even in strict mode"
    assert not mock_app.cell.agent_memory.notes


def test_the_kit_offers_remember_only_to_a_writer(on_mock: Niadra, mock_app: MockApp) -> None:
    reader = on_mock.tools(MARINA, conversation_id="c-1", agent_memory=True)
    assert reader.names[-1] == "search_agent_memory"
    assert json.loads(reader.call("remember", {"kind": "pitfall", "title": "t", "body": "b"}))[
        "error"
    ].startswith("unknown tool")
    writer = on_mock.tools(MARINA, conversation_id="c-1", agent_memory=True, write_agent_memory=True)
    saved = json.loads(writer.call("remember", {"kind": "pitfall", "title": "Refunds", "body": PROCEDURE}))
    assert saved["saved"] is True
    (note,) = mock_app.cell.agent_memory.notes.values()
    assert note.evidence is not None and note.evidence.conversation_id == "c-1"
    found = json.loads(writer.call("search_agent_memory", {"query": "refresh the invoice"}))
    assert [n["title"] for n in found["notes"]] == ["Refunds"]


def test_a_person_approves_what_an_agent_writes_in_human_only_spaces(
    on_mock: Niadra, mock_app: MockApp
) -> None:
    mock_app.cell.agent_memory.writes = "human_only"
    result = on_mock.remember("procedure", "Credit", PROCEDURE)
    assert result and result.note is None and result.proposal_id
    assert on_mock.agent_memory().text == ""


def test_off_down_or_missing_the_memory_is_empty_and_the_tools_say_so(
    on_mock: Niadra, lenient: Niadra, mock_app: MockApp, respx_mock: respx.MockRouter
) -> None:
    on_mock.remember("procedure", "Credit", PROCEDURE)
    mock_app.cell.agent_memory.enabled = False
    block = on_mock.agent_memory()
    assert (block.enabled, block.text, block.etag, block.error) == (False, "", "am-off", None)
    kit = on_mock.tools(MARINA, conversation_id="c-1", agent_memory=True, write_agent_memory=True)
    assert json.loads(kit.call("search_agent_memory", {"query": "credit"})) == {"notes": []}

    respx_mock.get(f"{BASE}/v1/agent-memory/block").respond(501)
    missing = lenient.agent_memory()
    assert (missing.enabled, missing.text, missing.error) == (False, "", "not_implemented")
    respx_mock.get(f"{BASE}/v1/agent-memory/block?max_tokens=100").respond(
        200, json={"text": "x", "etag": "e", "enabled": False}
    )
    assert lenient.agent_memory(100).text == "", "a block of a space with the memory off is never shown"
    respx_mock.post(f"{BASE}/v1/agent-memory/search").respond(503)
    assert lenient.search_agent_memory("credit") == []
    strict_kit = on_mock.tools(MARINA, conversation_id="c-1", agent_memory=True)
    mock_app.cell.fail_next("/v1/agent-memory/", 503, times=10)
    assert strict_kit.call("search_agent_memory", {"query": "credit"}) == MEMORY_UNAVAILABLE
    assert not Niadra().agent_memory().text


def test_a_new_space_has_the_memory_off_and_writing_needs_the_scope(respx_mock: respx.MockRouter) -> None:
    import httpx

    from niadra_mock import MOCK_KEY

    app = MockApp()
    http = httpx.Client(transport=httpx.WSGITransport(app=app.wsgi))
    writer = Niadra(MOCK_KEY, base_url="http://mock", http_client=http)
    reader = Niadra("nia_sk_test_local_mock_k2_reader", base_url="http://mock", http_client=http)
    assert (writer.agent_memory().etag, writer.agent_memory().enabled) == ("am-off", False)
    refused = reader.remember("pitfall", "Transfers", "Say the queue name first.")
    assert refused.error == "scope_missing" and not refused
    kit = reader.tools(MARINA, conversation_id="c-1", agent_memory=True, write_agent_memory=True)
    assert kit.call("remember", {"kind": "pitfall", "title": "t", "body": "b"}) == MEMORY_UNAVAILABLE
    assert writer.remember("pitfall", "Transfers", "Say the queue name first.").note is not None
    app.cell.enable_agent_memory()
    # A new request (another budget, so not the cached "off" block) sees the memory on.
    assert "Say the queue name first." in writer.agent_memory(250).text
    for client in (writer, reader):
        client.close()
    http.close()


async def test_the_async_client_and_a_sessions_view(on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    saved = await on_mock_async.remember("tool_note", "Voice refunds", PROCEDURE, tags=["voice"])
    assert saved.note is not None
    async with on_mock_async.conversation("call-1", subject=MARINA, channel="voice", view="voice") as call:
        block = await call.agent_memory()
        assert PROCEDURE in block.text
        noted = await call.remember("pitfall", "Transfers", "Warm transfers need the queue id first.")
        assert noted.note is not None and noted.note.evidence is not None
        assert noted.note.evidence.conversation_id == "call-1"
        kit = call.tools(agent_memory=True)
        assert kit is not None
        assert "Voice refunds" in await kit.call("search_agent_memory", {"query": "refunds"})


def test_admin_routes_of_the_emulator(on_mock: Niadra, mock_app: MockApp) -> None:
    from niadra._transport import Request

    def call(method: str, path: str, **kwargs: object) -> object:
        return on_mock._transport.request(Request(method, path, **kwargs))  # type: ignore[arg-type]

    saved = on_mock.remember("procedure", "Credit", PROCEDURE)
    assert saved.note is not None
    note_id = saved.note.note_id
    updated = call("PATCH", f"/v1/agent-memory/notes/{note_id}", json={"body": "Refresh the invoice last."})
    assert isinstance(updated, dict) and updated["version"] == 2 and updated["supersedes_note_id"] == note_id
    versions = call("GET", f"/v1/agent-memory/notes/{updated['note_id']}/versions")
    assert isinstance(versions, list) and [v["version"] for v in versions] == [1, 2]
    retired = call("POST", f"/v1/agent-memory/notes/{updated['note_id']}/retire")
    assert isinstance(retired, dict) and retired["status"] == "retired"
    on_mock.action("credit", subject=MARINA, object="invoice:erp:0823", conversation_id="c-9")
    on_mock.flush()
    proposal = call("POST", "/v1/agent-memory/distill", json={"source_id": "s", "conversation_id": "c-9"})
    assert isinstance(proposal, dict) and proposal["status"] == "pending" and "credit" in proposal["body"]
    approved = call("POST", f"/v1/agent-memory/proposals/{proposal['proposal_id']}/approve")
    assert isinstance(approved, dict) and approved["origin"] == "distilled"
    exported = call("GET", "/v1/agent-memory/export", params={"source_id": approved["source_id"]})
    assert isinstance(exported, dict) and len(exported["notes"]) == 3
    erased = call("DELETE", "/v1/agent-memory/notes", params={"source_id": approved["source_id"]})
    assert isinstance(erased, dict) and erased["deleted"] == 3


def test_the_pack_as_data(on_mock: Niadra) -> None:
    on_mock.track(
        {
            "conversation_id": "wa-1",
            "handles": [MARINA],
            "speaker": {"role": "customer"},
            "content": {"text": "hi"},
        }
    )
    on_mock.flush()
    text = on_mock.context(MARINA, conversation_id="c-2")
    assert text.pack is None
    data = on_mock.context(MARINA, conversation_id="c-3", format="json")
    assert data.pack is not None and data.pack.spec == "context-pack.v0"
    assert [s.name for s in data.pack.sections] == ["episodes"]
    assert data.pack.sections[0].lines[0].endswith("whatsapp customer: hi")
    assert data.pack.stamp.etag == data.etag


def _turn(niadra: Niadra, text: str, *, days_ago: int, conversation: str, **extra: object) -> None:
    niadra.track(
        {
            "conversation_id": conversation,
            "handles": [MARINA],
            "speaker": {"role": "customer"},
            "content": {"text": text},
            "occurred_at": datetime.now(timezone.utc) - timedelta(days=days_ago),
            **extra,
        }
    )


def test_time_words_expiry_and_versions(on_mock: Niadra) -> None:
    _turn(on_mock, "the router is broken", days_ago=0, conversation="a")
    _turn(on_mock, "the router again", days_ago=40, conversation="b")
    offer_end = datetime.now(timezone.utc) - timedelta(hours=1)
    _turn(on_mock, "router discount offer", days_ago=1, conversation="c", valid_until=offer_end)
    on_mock.flush()

    today = on_mock.search(MARINA, "router", filters={"when": "hoje"})
    assert [i.text for i in today.items] == ["customer: the router is broken"]
    assert today.window is not None and today.window.since is not None and today.ignored == []
    unread = on_mock.search(MARINA, "router", filters={"when": "when the moon was full"})
    assert unread.ignored == ["when"] and len(unread.items) == 2, "the expired offer is left out"
    expired = on_mock.search(MARINA, "router", filters={"show_expired": True})
    assert len(expired.items) == 3
    assert any(i.valid_until is not None for i in expired.items)
    page = on_mock.timeline(MARINA, filters={"when": "last 7 days"})
    assert len(page.items) == 1 and page.window is not None

    kit = on_mock.tools(MARINA)
    flat = json.loads(
        kit.call("search_customer_history", {"query": "router", "since": "2000-01-01T00:00:00Z"})
    )
    nested = json.loads(
        kit.call("search_customer_history", {"query": "router", "filters": {"when": "today"}})
    )
    assert len(flat["items"]) == 2 and len(nested["items"]) == 1
    legacy = json.loads(kit.call("get_customer_timeline", {"item_kinds": ["system_event"]}))
    assert legacy["items"] == [], "0.1.1's system_event reads as object"

    on_mock.track(
        {
            "kind": "system_event",
            "canonical_type": "invoice.issued",
            "object_refs": ["invoice:erp:0823"],
            "handles": [MARINA],
            "speaker": {"role": "system"},
        }
    )
    on_mock.track(
        {
            "kind": "system_event",
            "canonical_type": "invoice.paid",
            "object_refs": ["invoice:erp:0823"],
            "handles": [MARINA],
            "speaker": {"role": "system"},
        }
    )
    on_mock.flush()
    item = on_mock.search(MARINA, "invoice", filters={"item_kinds": ["object"]}).items[0]
    opened = on_mock.open(item.id, subject=MARINA)
    assert opened is not None and [v.version for v in opened.versions] == [1, 2]
