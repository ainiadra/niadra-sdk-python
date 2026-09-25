"""Retell AI: webhooks and custom functions recorded in the platform's public format, signatures
computed here the way Retell's SDK computes them, a fixed clock, and the emulator behind."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from niadra import AsyncNiadra, Niadra
from niadra.integrations.retell import RetellWebhooks, tool_configs, verify_signature
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, events, items, seed, seed_async, turns

PAYLOADS = Path(__file__).parent / "payloads"
CALL = "call_8f1e2d3c4b5a69788f1e2d3c4b"
API_KEY = "key_retell_test_webhook"
NOW = 1790301720


def payload(name: str) -> bytes:
    return (PAYLOADS / f"retell_{name}.json").read_bytes()


def signed(body: bytes, *, at_ms: int = NOW * 1000, key: str = API_KEY) -> dict[str, str]:
    digest = hmac.new(key.encode(), body + str(at_ms).encode(), hashlib.sha256).hexdigest()
    return {"X-Retell-Signature": f"v={at_ms},d={digest}"}


def hooks(niadra: Niadra | AsyncNiadra, **options: Any) -> RetellWebhooks:
    return RetellWebhooks(niadra, api_key=API_KEY, clock=lambda: NOW + 30, **options)


def custom_function(name: str, arguments: dict[str, Any], **call: Any) -> bytes:
    body = json.loads(payload("custom_function"))
    body["name"], body["args"] = name, arguments
    body["call"].update(call)
    return json.dumps(body).encode()


async def test_inbound_answers_the_callers_context_as_dynamic_variables(on_mock_async: AsyncNiadra) -> None:
    await seed_async(on_mock_async)
    body = payload("call_inbound")
    answer = await hooks(on_mock_async, override_agent_id="agent_2").inbound(body, signed(body))
    assert answer.status == 200
    inbound = answer.body["call_inbound"]
    assert inbound["override_agent_id"] == "agent_2"
    variables = inbound["dynamic_variables"]
    assert EARLIER in variables["niadra_context"]
    assert variables["niadra_context_etag"] and variables["niadra_injected_at"]
    assert all(isinstance(value, str) for value in variables.values()), "Retell takes string variables"


async def test_inbound_verifies_the_attestation_before_reading(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    body = payload("call_inbound")
    retell = hooks(on_mock_async, attestation=lambda call: call["custom_sip_headers"]["x-stir-verstat"])
    await retell.inbound(body, signed(body))
    assert [(v.method, v.level.value) for v in items(mock_app.cell, "verify")] == [
        ("network_attestation", "V2")
    ]
    assert mock_app.cell.level(CALL).value == "V2"


async def test_every_endpoint_refuses_a_bad_or_stale_signature(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    retell = hooks(on_mock_async)
    for method, name in ((retell.inbound, "call_inbound"), (retell.webhook, "call_ended")):
        body = payload(name)
        assert (await method(body, {})).status == 401
        assert (await method(body, signed(body, key="other"))).status == 401
        assert (await method(body, signed(body, at_ms=(NOW - 6 * 60) * 1000))).status == 401
        assert (await method(body + b" ", signed(body))).status == 401
    tool = custom_function("search_customer_history", {"query": "lid"})
    assert (await retell.custom_function(tool, {"x-retell-signature": "v=1,d=00"})).status == 401
    assert (await RetellWebhooks(on_mock_async, api_key=None).inbound(tool, signed(tool))).status == 401
    assert not mock_app.cell.items and not mock_app.cell.events


async def test_inbound_with_niadra_down_still_lets_the_call_start(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    body = payload("call_inbound")
    answer = await hooks(on_mock_async).inbound(body, signed(body))
    assert answer.status == 200
    assert answer.body["call_inbound"]["dynamic_variables"]["niadra_context"] == ""
    bad = b'{"event": "call_inbound"}'
    assert (await hooks(on_mock_async).inbound(bad, signed(bad))).status == 400


def test_tool_configs_are_the_kits_definitions_word_for_word() -> None:
    configs = tool_configs("https://agent.example.com/retell/tools")
    assert [c["name"] for c in configs] == list(DEFINITIONS)
    for config in configs:
        definition = DEFINITIONS[config["name"]]
        assert (config["description"], config["parameters"]) == (
            definition["description"],
            definition["parameters"],
        )
        assert (config["type"], config["method"], config["url"]) == (
            "custom",
            "POST",
            "https://agent.example.com/retell/tools",
        )


async def test_a_custom_function_reads_the_history_of_the_customer_on_the_call(
    on_mock_async: AsyncNiadra,
) -> None:
    await seed_async(on_mock_async)
    retell = hooks(on_mock_async)
    body = payload("custom_function")
    answer = await retell.custom_function(body, signed(body))
    assert answer.status == 200 and "4471" in json.dumps(answer.body)

    unknown = custom_function("delete_everything", {})
    assert (await retell.custom_function(unknown, signed(unknown))).status == 404
    withheld = custom_function("search_customer_history", {"query": "lid"}, from_number="anonymous")
    answer = await retell.custom_function(withheld, signed(withheld))
    assert "no customer" in answer.body["error"]


async def test_an_outbound_call_reads_the_called_customer(on_mock_async: AsyncNiadra) -> None:
    await seed_async(on_mock_async)
    retell = hooks(on_mock_async)
    extra = await retell.outbound("+5511912345678", conversation_id="campaign-7")
    assert EARLIER in extra["retell_llm_dynamic_variables"]["niadra_context"]
    assert extra["metadata"] == {"niadra_conversation_id": "campaign-7"}
    # The tool call of that call: Retell's own call id, the customer on the called side.
    body = custom_function(
        "search_customer_history",
        {"query": "lid"},
        direction="outbound",
        from_number="+551130001000",
        to_number="+5511912345678",
        metadata=extra["metadata"],
    )
    answer = await retell.custom_function(body, signed(body))
    assert "4471" in json.dumps(answer.body)


async def test_a_custom_function_with_niadra_down_answers_the_model(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    body = payload("custom_function")
    answer = await hooks(on_mock_async).custom_function(body, signed(body))
    assert answer.status == 200 and "unavailable" in answer.body["error"]


async def test_call_ended_records_the_turns_the_transfer_and_the_end(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    body = payload("call_ended")
    retell = hooks(on_mock_async)
    assert (await retell.webhook(body, signed(body))).status == 200
    assert turns(mock_app.cell, CALL) == [
        ("ai_agent", "Hi Marina, is this about the lid of order 4471?"),
        ("customer", "Yes, it still has not arrived."),
        ("ai_agent", "I will transfer you to logistics."),
    ]
    recorded = [e for e in events(mock_app.cell, CALL) if e.kind.value == "message"]
    assert [e.occurred_at.timestamp() for e in recorded] == [1790301601, 1790301605, 1790301611]
    assert recorded[0].context_stamp is not None and recorded[0].context_stamp.etag == "etag-9"
    assert recorded[1].handles[0].value == "+5511912345678"
    assert [(h.target, h.reason) for h in items(mock_app.cell, "handoff")] == [("human", "call_transfer")]
    assert CALL in mock_app.cell.ended

    assert (await retell.webhook(body, signed(body))).status == 200
    assert len(turns(mock_app.cell, CALL)) == 3, "a redelivered event records nothing twice"


async def test_other_events_answer_200_and_record_nothing(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    for event in ("call_started", "call_analyzed", "transfer_started"):
        body = json.dumps({"event": event, "call": {"call_id": CALL}}).encode()
        assert (await hooks(on_mock_async).webhook(body, signed(body))).status == 200
    bad = b"[]"
    assert (await hooks(on_mock_async).webhook(bad, signed(bad))).status == 400
    await on_mock_async.flush()
    assert not mock_app.cell.items and not mock_app.cell.events


async def test_niadra_down_never_fails_the_webhook(on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    body = payload("call_ended")
    assert (await hooks(on_mock_async).webhook(body, signed(body))).status == 200


def test_the_sync_client_gets_the_same_answers(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    retell = hooks(on_mock)
    inbound = payload("call_inbound")
    answer = retell.inbound_sync(inbound, signed(inbound))
    assert EARLIER in answer.body["call_inbound"]["dynamic_variables"]["niadra_context"]
    tool = payload("custom_function")
    assert "4471" in json.dumps(retell.custom_function_sync(tool, signed(tool)).body)
    assert EARLIER in retell.outbound_sync("+5511912345678")["retell_llm_dynamic_variables"]["niadra_context"]
    ended = payload("call_ended")
    assert retell.webhook_sync(ended, signed(ended)).status == 200
    assert CALL in mock_app.cell.ended


async def test_the_async_client_cannot_be_driven_from_the_sync_twin(on_mock_async: AsyncNiadra) -> None:
    body = payload("call_inbound")
    with pytest.raises(TypeError):
        hooks(on_mock_async).inbound_sync(body, signed(body))


def test_verify_signature_windows() -> None:
    body = b"{}"
    header = signed(body)["X-Retell-Signature"]
    assert verify_signature(body, header, API_KEY, now_ms=NOW * 1000)
    assert verify_signature(body, header, API_KEY, now_ms=(NOW + 4 * 60) * 1000)
    assert not verify_signature(body, header, API_KEY, now_ms=(NOW + 6 * 60) * 1000)
    assert not verify_signature(body, "v=abc,d=00", API_KEY, now_ms=NOW * 1000)
    assert not verify_signature(body, header, None, now_ms=NOW * 1000)


async def test_the_agents_notes_as_a_dynamic_variable_and_the_memory_tools(
    on_mock_async: AsyncNiadra,
) -> None:
    await seed_async(on_mock_async)
    await on_mock_async.remember("pitfall", "Transfers", "Say the queue name before a warm transfer.")
    retell = hooks(on_mock_async, agent_memory={"write": True})
    inbound = payload("call_inbound")
    variables = (await retell.inbound(inbound, signed(inbound))).body["call_inbound"]["dynamic_variables"]
    assert "warm transfer" in variables["niadra_agent_memory"] and EARLIER in variables["niadra_context"]
    names = [c["name"] for c in tool_configs("u", agent_memory={"write": True})]
    assert names[-2:] == ["search_agent_memory", "remember"]
    body = custom_function("search_agent_memory", {"query": "warm transfer"})
    assert (await retell.custom_function(body, signed(body))).body["notes"][0]["title"] == "Transfers"
    off = await hooks(on_mock_async).inbound(inbound, signed(inbound))
    assert off.body["call_inbound"]["dynamic_variables"]["niadra_agent_memory"] == ""
    assert (await hooks(on_mock_async).custom_function(body, signed(body))).status == 404
