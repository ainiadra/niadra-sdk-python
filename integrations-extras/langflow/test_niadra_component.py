"""The Langflow components with Langflow's own component runtime (`lfx`) and the emulator behind."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("lfx")
sys.path.insert(0, str(Path(__file__).parent))

import niadra_component
from niadra_component import NiadraContextComponent, NiadraRecordReplyComponent, NiadraSearchHistoryComponent

from niadra import Niadra, phone
from niadra.options import CacheOptions, QueueOptions
from niadra_mock import MOCK_KEY, MockApp

MARINA = phone("+5511912345678")
EARLIER = "The lid of order 4471 arrived broken, I need a new one by Friday"
CONNECTION = {
    "api_key": MOCK_KEY,
    "channel": "chat",
    "handle_type": "phone",
    "handle_value": "+5511912345678",
}


@pytest.fixture
def mock_app() -> MockApp:
    app = MockApp()
    app.cell.enable_agent_memory()
    return app


@pytest.fixture
def client(mock_app: MockApp, monkeypatch: pytest.MonkeyPatch) -> Iterator[Niadra]:
    http = httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi))
    niadra = Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="chat",
        queue=QueueOptions(batch_size=10_000, interval=3600, turn_interval=3600),
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=http,
    )
    niadra.track(
        {
            "channel": "whatsapp",
            "conversation_id": "wa-1",
            "handles": [MARINA],
            "speaker": {"role": "customer"},
            "content": {"text": EARLIER},
        }
    )
    niadra.flush()
    monkeypatch.setattr(niadra_component, "_client", lambda *args: niadra)
    yield niadra
    niadra.close()
    http.close()


def build(component: Any, **values: Any) -> Any:
    component.set(**{**CONNECTION, **values})
    return component


def texts(mock_app: MockApp, conversation: str) -> list[tuple[str, str]]:
    return [
        (e.item.speaker.role.value, e.text)
        for e in mock_app.cell.events
        if e.item.conversation_id == conversation
    ]


def test_the_context_records_the_message_and_carries_the_pack(client: Niadra, mock_app: MockApp) -> None:
    client.remember("procedure", "Replacement parts", "Open a replacement order before any refund.")
    component = build(
        NiadraContextComponent(), conversation_id="flow-1", customer_message="About my lid", agent_memory=True
    )
    context = component.build_context()
    assert context.text.index("replacement order") < context.text.index(EARLIER)
    reply = build(
        NiadraRecordReplyComponent(), conversation_id="flow-1", agent_message="Your new lid ships today."
    )
    assert reply.record_reply().text == "Your new lid ships today."
    client.flush()
    assert texts(mock_app, "flow-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]


def test_the_search_is_a_tool_whose_customer_the_flow_sets(client: Niadra) -> None:
    component = build(NiadraSearchHistoryComponent(), conversation_id="flow-2", query="lid")
    assert "4471" in component.search_history().text
    tool_inputs = [i.name for i in component.inputs if getattr(i, "tool_mode", False)]
    assert tool_inputs == ["query", "when"], "the model fills the query and the period, never the customer"


def test_niadra_down_never_fails_the_flow(client: Niadra, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    assert build(NiadraContextComponent(), conversation_id="flow-3").build_context().text == ""
    result = build(NiadraSearchHistoryComponent(), conversation_id="flow-3", query="lid").search_history()
    assert "unavailable" in json.loads(result.text)["error"]
