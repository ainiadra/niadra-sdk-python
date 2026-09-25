"""The Dify plugin with Dify's own plugin SDK (`dify_plugin`): its manifests read by Dify's models,
its classes loaded by Dify's loader, its tools invoked as Dify invokes them, and the emulator behind."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("dify_plugin")
PLUGIN = Path(__file__).parent
sys.path.insert(0, str(PLUGIN))

import yaml
from dify_plugin import Tool, ToolProvider
from dify_plugin.core.entities.plugin.setup import PluginConfiguration
from dify_plugin.core.utils.class_loader import load_single_subclass_from_source
from dify_plugin.entities.tool import ToolProviderConfiguration
from dify_plugin.errors.tool import ToolProviderCredentialValidationError
from tools import niadra_base

from niadra import Niadra, phone
from niadra.options import CacheOptions, QueueOptions
from niadra.tools import BUILTIN_DEFINITIONS, definitions
from niadra_mock import MOCK_KEY, MockApp

MARINA = phone("+5511912345678")
EARLIER = "The lid of order 4471 arrived broken, I need a new one by Friday"
CUSTOMER = {
    "customer_id_type": "phone",
    "customer_id": "+5511912345678",
    "conversation_id": "dify-1",
    "channel": "chat",
}
KIT = {d["function"]["name"]: d["function"] for d in definitions(agent_memory=True)}
TOOLS = [
    "get_context",
    "record_reply",
    "search_customer_history",
    "get_customer_timeline",
    "open_history_item",
    "search_agent_memory",
]


@pytest.fixture
def mock_app() -> MockApp:
    app = MockApp()
    app.cell.enable_agent_memory()
    return app


def on(mock_app: MockApp, key: str = MOCK_KEY, strict: bool = False) -> tuple[Niadra, httpx.Client]:
    http = httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi))
    niadra = Niadra(
        key,
        strict=strict,
        base_url="http://mock",
        channel="chat",
        queue=QueueOptions(batch_size=10_000, interval=3600, turn_interval=3600),
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=http,
    )
    return niadra, http


@pytest.fixture
def client(mock_app: MockApp, monkeypatch: pytest.MonkeyPatch) -> Iterator[Niadra]:
    niadra, http = on(mock_app)
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
    monkeypatch.setattr(niadra_base, "client", lambda *args: niadra)
    yield niadra
    niadra.close()
    http.close()


def tool(name: str) -> Any:
    cls = load_single_subclass_from_source(
        module_name=f"tools.{name}", script_path=str(PLUGIN / "tools" / f"{name}.py"), parent_type=Tool
    )
    return cls.from_credentials({"api_key": MOCK_KEY})


def invoke(name: str, **parameters: Any) -> tuple[list[str], list[Any]]:
    """The text and JSON messages a tool yields."""
    texts, jsons = [], []
    for message in tool(name).invoke({**CUSTOMER, **parameters}):
        if message.type.value == "text":
            texts.append(message.message.text)
        elif message.type.value == "json":
            jsons.append(message.message.json_object)
    return texts, jsons


def turns(mock_app: MockApp, conversation: str) -> list[tuple[str, str]]:
    return [
        (e.item.speaker.role.value, e.text)
        for e in mock_app.cell.events
        if e.item.conversation_id == conversation and e.item.kind.value == "message"
    ]


def test_the_manifests_are_valid_for_dify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(PLUGIN)
    manifest = PluginConfiguration(**yaml.safe_load((PLUGIN / "manifest.yaml").read_text()))
    assert manifest.plugins.tools == ["provider/niadra.yaml"]
    provider = ToolProviderConfiguration(**yaml.safe_load((PLUGIN / "provider/niadra.yaml").read_text()))
    assert [t.identity.name for t in provider.tools] == TOOLS
    assert (PLUGIN / "_assets" / manifest.icon).exists()
    loaded = load_single_subclass_from_source(
        module_name="provider.niadra",
        script_path=str(PLUGIN / "provider/niadra.py"),
        parent_type=ToolProvider,
    )
    assert loaded.__name__ == "NiadraProvider"
    for spec in provider.tools:
        assert spec.extra.python.source == f"tools/{spec.identity.name}.py"
        assert tool(spec.identity.name) is not None


def test_the_model_sees_the_kits_words_and_never_the_customer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(PLUGIN)
    provider = ToolProviderConfiguration(**yaml.safe_load((PLUGIN / "provider/niadra.yaml").read_text()))
    for spec in provider.tools:
        by_model = {p.name: p for p in spec.parameters if p.form.value == "llm"}
        set_by_app = {p.name for p in spec.parameters if p.form.value == "form"}
        assert set(CUSTOMER) <= set_by_app, "the customer is set by the app, never by the model"
        definition = KIT.get(spec.identity.name)
        if definition is None:
            assert not by_model
            continue
        assert spec.description.llm == definition["description"]
        properties = definition["parameters"]["properties"]
        for name, parameter in by_model.items():
            schema = properties.get(name) or properties["filters"]["properties"][name]
            assert parameter.llm_description == schema.get("description")


def test_get_context_records_the_message_and_returns_the_context(client: Niadra, mock_app: MockApp) -> None:
    texts, jsons = invoke("get_context", customer_message="About my lid")
    assert EARLIER in texts[0]
    assert jsons[0]["context"] == texts[0]
    assert jsons[0]["etag"]
    assert turns(mock_app, "dify-1") == [("customer", "About my lid")]
    _, recorded = invoke("record_reply", agent_message="Your new lid ships today.")
    assert recorded == [{"recorded": True}]
    assert turns(mock_app, "dify-1")[-1] == ("ai_agent", "Your new lid ships today.")


def test_the_history_tools_answer_for_the_configured_customer(client: Niadra) -> None:
    texts, jsons = invoke("search_customer_history", query="lid", when="this week")
    assert "4471" in texts[0]
    assert isinstance(jsons[0], dict)
    texts, _ = invoke("get_customer_timeline", limit=5)
    assert "items" in json.loads(texts[0])
    anonymous, _ = invoke("search_customer_history", query="lid", customer_id="")
    assert "no customer" in anonymous[0]


def test_the_agents_notes(client: Niadra) -> None:
    client.remember("procedure", "Replacement parts", "Open a replacement order before any refund.")
    texts, _ = invoke("get_context", agent_memory=True)
    assert texts[0].index("replacement order") < texts[0].index(EARLIER)
    notes, _ = invoke("search_agent_memory", query="refund")
    assert "Replacement parts" in notes[0]


def test_niadra_down_never_fails_a_tool(client: Niadra, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    texts, _ = invoke("get_context", customer_message="Hi")
    assert texts == [""]
    searched, _ = invoke("search_customer_history", query="lid")
    assert "unavailable" in searched[0]


def test_credentials_are_checked_against_the_api(mock_app: MockApp, monkeypatch: pytest.MonkeyPatch) -> None:
    provider_class = load_single_subclass_from_source(
        module_name="provider.niadra",
        script_path=str(PLUGIN / "provider/niadra.py"),
        parent_type=ToolProvider,
    )
    opened: list[httpx.Client] = []

    def fake(key: str, *, strict: bool = False, **_: Any) -> Niadra:
        niadra, http = on(mock_app, key, strict)
        opened.append(http)
        return niadra

    # Dify loads the provider's file as a module of its own; its globals are where `Niadra` is read.
    monkeypatch.setitem(provider_class._validate_credentials.__globals__, "Niadra", fake)
    provider = provider_class()
    provider.validate_credentials({"api_key": MOCK_KEY})
    mock_app.cell.revoke("nia_sk_live_br1_acme_k9_revoked")
    with pytest.raises(ToolProviderCredentialValidationError):
        provider.validate_credentials({"api_key": "nia_sk_live_br1_acme_k9_revoked"})
    with pytest.raises(ToolProviderCredentialValidationError):
        provider.validate_credentials({"api_key": ""})
    for http in opened:
        http.close()


def test_the_kit_definitions_are_the_sdks() -> None:
    assert {d["function"]["name"] for d in BUILTIN_DEFINITIONS} <= set(TOOLS)
