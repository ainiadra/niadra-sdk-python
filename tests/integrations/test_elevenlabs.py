"""ElevenLabs: the three webhooks with payloads recorded in the platform's public format, signatures
computed here, a fixed clock, and the emulator behind."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from niadra import AsyncNiadra, Niadra
from niadra.integrations.elevenlabs import ElevenLabsWebhooks, tool_configs, verify_signature
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, events, items, seed, seed_async, turns

PAYLOADS = Path(__file__).parent / "payloads"
CALL = "CA5f1c2d7e9b3a4f6e8d0c1b2a3f4e5d6c"
SECRET = "wsec_test_post_call"
SHARED = "shared-test-secret"
NOW = 1790301720


def payload(name: str) -> bytes:
    return (PAYLOADS / f"elevenlabs_{name}.json").read_bytes()


def signed(body: bytes, *, at: int = NOW, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
    return {"ElevenLabs-Signature": f"t={at},v0={digest}"}


def hooks(niadra: Niadra | AsyncNiadra, **options: Any) -> ElevenLabsWebhooks:
    return ElevenLabsWebhooks(
        niadra, webhook_secret=SECRET, shared_secret=SHARED, clock=lambda: NOW + 60, **options
    )


AUTH = {"x-niadra-secret": SHARED}


async def test_initiation_answers_the_callers_context(on_mock_async: AsyncNiadra) -> None:
    await seed_async(on_mock_async)
    answer = await hooks(on_mock_async).conversation_initiation(payload("initiation"), AUTH)
    assert answer.status == 200
    assert answer.body["type"] == "conversation_initiation_client_data"
    variables = answer.body["dynamic_variables"]
    assert EARLIER in variables["niadra_context"]
    assert variables["niadra_context_etag"] and variables["niadra_injected_at"]


async def test_initiation_verifies_the_attestation_before_reading(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    await seed_async(on_mock_async)
    webhooks = hooks(on_mock_async, attestation=lambda _payload: "A")
    await webhooks.conversation_initiation(payload("initiation"), AUTH)
    assert [(v.method, v.level.value) for v in items(mock_app.cell, "verify")] == [
        ("network_attestation", "V2")
    ]
    assert mock_app.cell.level(CALL).value == "V2"


async def test_initiation_and_tools_refuse_a_request_without_the_shared_secret(
    on_mock_async: AsyncNiadra,
) -> None:
    webhooks = hooks(on_mock_async)
    assert (await webhooks.conversation_initiation(payload("initiation"))).status == 401
    wrong = {"X-Niadra-Secret": "guess"}
    assert (await webhooks.conversation_initiation(payload("initiation"), wrong)).status == 401
    body = {"query": "lid", "system__call_sid": CALL, "system__caller_id": "+5511912345678"}
    assert (await webhooks.server_tool("search_customer_history", json.dumps(body), wrong)).status == 401
    unset = ElevenLabsWebhooks(on_mock_async)
    assert (await unset.conversation_initiation(payload("initiation"), AUTH)).status == 401


async def test_initiation_with_niadra_down_still_lets_the_call_start(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    answer = await hooks(on_mock_async).conversation_initiation(payload("initiation"), AUTH)
    assert answer.status == 200
    assert answer.body["dynamic_variables"]["niadra_context"] == ""
    assert (await hooks(on_mock_async).conversation_initiation(b"not json", AUTH)).status == 400


def test_tool_configs_carry_the_kits_words_and_the_call_from_system_variables() -> None:
    configs = tool_configs("https://agent.example.com/niadra/tools/", secret=SHARED)
    assert [c["name"] for c in configs] == list(DEFINITIONS)
    for config in configs:
        definition = DEFINITIONS[config["name"]]
        assert config["type"] == "webhook"
        assert config["description"] == definition["description"]
        schema = config["api_schema"]
        assert schema["url"] == f"https://agent.example.com/niadra/tools/{config['name']}"
        assert schema["request_headers"] == {"X-Niadra-Secret": SHARED}
        body = schema["request_body_schema"]
        model_facing = {k: v for k, v in body["properties"].items() if "dynamic_variable" not in v}
        assert set(model_facing) == set(definition["parameters"]["properties"])
        for name, prop in model_facing.items():
            expected = definition["parameters"]["properties"][name]
            assert prop["description"] == expected.get("description", "")
        filled = {k: v["dynamic_variable"] for k, v in body["properties"].items() if "dynamic_variable" in v}
        assert filled == {k: k for k in ("system__call_sid", "system__conversation_id", "system__caller_id")}
        assert body["required"] == definition["parameters"].get("required", [])


async def test_a_server_tool_reads_the_history_of_the_caller_on_the_line(on_mock_async: AsyncNiadra) -> None:
    await seed_async(on_mock_async)
    body = {"query": "lid", "system__call_sid": CALL, "system__caller_id": "+5511912345678"}
    answer = await hooks(on_mock_async).server_tool("search_customer_history", json.dumps(body), AUTH)
    assert answer.status == 200
    assert "4471" in json.dumps(answer.body)

    unknown = await hooks(on_mock_async).server_tool("delete_everything", json.dumps(body), AUTH)
    assert unknown.status == 404
    anonymous = {"query": "lid", "system__call_sid": CALL, "system__caller_id": "anonymous"}
    answer = await hooks(on_mock_async).server_tool("search_customer_history", json.dumps(anonymous), AUTH)
    assert "no customer" in answer.body["error"]


async def test_a_server_tool_with_niadra_down_answers_the_model(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    body = {"query": "lid", "system__call_sid": CALL, "system__caller_id": "+5511912345678"}
    answer = await hooks(on_mock_async).server_tool("search_customer_history", json.dumps(body), AUTH)
    assert answer.status == 200 and "unavailable" in answer.body["error"]


async def test_post_call_records_the_turns_the_transfer_and_the_end(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    body = payload("post_call")
    answer = await hooks(on_mock_async).post_call(body, signed(body))
    assert answer.status == 200
    assert turns(mock_app.cell, CALL) == [
        ("ai_agent", "Hi Marina, is this about the lid of order 4471?"),
        ("customer", "Yes, it still has not arrived."),
        ("ai_agent", "Let me put you through to a person in logistics."),
    ]
    recorded = [e for e in events(mock_app.cell, CALL) if e.kind.value == "message"]
    assert [e.occurred_at.timestamp() for e in recorded] == [1790301600, 1790301604, 1790301609]
    first = recorded[0]
    assert first.usage is not None
    assert (first.usage.provider, first.usage.model) == ("google", "gemini-2.5-flash")
    assert (first.usage.prompt_tokens, first.usage.cached_tokens) == (1336, 1024)
    assert first.context_stamp is not None and first.context_stamp.etag == "etag-7"
    assert recorded[1].handles[0].value == "+5511912345678"
    assert [(h.target, h.reason) for h in items(mock_app.cell, "handoff")] == [
        ("human", "transfer_to_number")
    ]
    assert CALL in mock_app.cell.ended

    again = await hooks(on_mock_async).post_call(body, signed(body))
    assert again.status == 200
    assert len(turns(mock_app.cell, CALL)) == 3, "a redelivered webhook records nothing twice"


async def test_post_call_refuses_a_bad_or_stale_signature(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    body = payload("post_call")
    webhooks = hooks(on_mock_async)
    assert (await webhooks.post_call(body, {})).status == 401
    assert (await webhooks.post_call(body, signed(body, secret="other"))).status == 401
    assert (await webhooks.post_call(body, signed(body, at=NOW - 31 * 60))).status == 401
    assert (await webhooks.post_call(body + b" ", signed(body))).status == 401
    assert not mock_app.cell.events


async def test_post_call_ignores_other_events(on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    body = json.dumps({"type": "post_call_audio", "data": {"conversation_id": "c"}}).encode()
    assert (await hooks(on_mock_async).post_call(body, signed(body))).status == 200
    assert not mock_app.cell.items


def test_the_sync_client_gets_the_same_answers(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    webhooks = hooks(on_mock)
    answer = webhooks.conversation_initiation_sync(payload("initiation"), AUTH)
    assert EARLIER in answer.body["dynamic_variables"]["niadra_context"]
    body = {"query": "lid", "system__call_sid": CALL, "system__caller_id": "+5511912345678"}
    assert "4471" in json.dumps(webhooks.server_tool_sync("search_customer_history", body, AUTH).body)
    post = payload("post_call")
    assert webhooks.post_call_sync(post, signed(post)).status == 200
    assert CALL in mock_app.cell.ended


async def test_the_async_client_cannot_be_driven_from_the_sync_twin(on_mock_async: AsyncNiadra) -> None:
    with pytest.raises(TypeError):
        hooks(on_mock_async).conversation_initiation_sync(payload("initiation"), AUTH)


def test_verify_signature_windows() -> None:
    body = b"{}"
    header = signed(body)["ElevenLabs-Signature"]
    assert verify_signature(body, header, SECRET, now=NOW)
    assert verify_signature(body, header, SECRET, now=NOW + 29 * 60)
    assert not verify_signature(body, header, SECRET, now=NOW + 31 * 60)
    assert not verify_signature(body, "t=abc,v0=00", SECRET, now=NOW)
    assert not verify_signature(body, header, None, now=NOW)


async def test_the_agents_notes_as_a_dynamic_variable_and_the_memory_tools(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    await seed_async(on_mock_async)
    await on_mock_async.remember("pitfall", "Transfers", "Say the queue name before a warm transfer.")
    webhooks = hooks(on_mock_async, agent_memory={"write": True})
    variables = (await webhooks.conversation_initiation(payload("initiation"), AUTH)).body[
        "dynamic_variables"
    ]
    assert "warm transfer" in variables["niadra_agent_memory"] and EARLIER in variables["niadra_context"]
    names = [c["name"] for c in tool_configs("https://a.example.com/t", agent_memory={"write": True})]
    assert names[-2:] == ["search_agent_memory", "remember"]
    body = {"query": "warm transfer", "system__call_sid": CALL, "system__caller_id": "+5511912345678"}
    found = await webhooks.server_tool("search_agent_memory", json.dumps(body), AUTH)
    assert found.body["notes"][0]["title"] == "Transfers"
    refused = await webhooks.server_tool(
        "remember",
        json.dumps({"kind": "pitfall", "title": "x", "body": "call +55 11 91234-5678", **body}),
        AUTH,
    )
    assert refused.body["error"] == "personal_data"
    off = await hooks(on_mock_async).conversation_initiation(payload("initiation"), AUTH)
    assert off.body["dynamic_variables"]["niadra_agent_memory"] == ""
    assert (await hooks(on_mock_async).server_tool("remember", json.dumps(body), AUTH)).status == 404
