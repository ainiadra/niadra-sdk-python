"""Vapi: server messages recorded in the platform's public format, and the emulator behind."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from niadra import AsyncNiadra, Niadra
from niadra.integrations.vapi import VapiServer, tool_definitions
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, events, items, seed, seed_async, turns

PAYLOADS = Path(__file__).parent / "payloads"
CALL = "3f9d2c1e-8b7a-4c6d-9e0f-1a2b3c4d5e6f"
SECRET = "vapi-server-secret"
AUTH = {"X-Vapi-Secret": SECRET}


def payload(name: str) -> bytes:
    return (PAYLOADS / f"vapi_{name}.json").read_bytes()


def server(niadra: Niadra | AsyncNiadra, **options: Any) -> VapiServer:
    return VapiServer(niadra, secret=SECRET, **options)


async def test_assistant_request_answers_the_saved_assistant_with_the_context(
    on_mock_async: AsyncNiadra,
) -> None:
    await seed_async(on_mock_async)
    answer = await server(on_mock_async, assistant_id="asst_1").handle(payload("assistant_request"), AUTH)
    assert answer.status == 200
    assert answer.body["assistantId"] == "asst_1"
    variables = answer.body["assistantOverrides"]["variableValues"]
    assert EARLIER in variables["niadra_context"]
    assert variables["niadra_context_etag"]


async def test_a_transient_assistant_gets_the_pack_after_its_instructions(on_mock_async: AsyncNiadra) -> None:
    await seed_async(on_mock_async)
    assistant: dict[str, Any] = {
        "name": "Acme",
        "model": {
            "provider": "openai",
            "model": "gpt-4.1",
            "messages": [{"role": "system", "content": "You are Acme's agent."}],
            "tools": tool_definitions("https://agent.example.com/vapi"),
        },
    }
    answer = await server(on_mock_async, assistant=assistant).handle(payload("assistant_request"), AUTH)
    messages = answer.body["assistant"]["model"]["messages"]
    assert messages[0] == {"role": "system", "content": "You are Acme's agent."}
    assert messages[1]["role"] == "system" and EARLIER in messages[1]["content"]
    assert assistant["model"]["messages"] == [{"role": "system", "content": "You are Acme's agent."}]


async def test_the_attestation_is_verified_before_the_first_context(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    vapi = server(on_mock_async, assistant_id="asst_1", attestation=lambda _message: "C")
    await vapi.handle(payload("assistant_request"), AUTH)
    assert [(v.method, v.level.value) for v in items(mock_app.cell, "verify")] == [
        ("network_attestation", "V1")
    ]


def test_tool_definitions_are_the_kits_word_for_word() -> None:
    tools = tool_definitions("https://agent.example.com/vapi", secret=SECRET)
    assert {t["function"]["name"]: t["function"] for t in tools} == DEFINITIONS
    assert all(t["type"] == "function" and t["server"]["url"].endswith("/vapi") for t in tools)


async def test_tool_calls_run_for_the_caller_and_leave_other_tools_alone(on_mock_async: AsyncNiadra) -> None:
    await seed_async(on_mock_async)
    answer = await server(on_mock_async).handle(payload("tool_calls"), AUTH)
    ours, theirs = answer.body["results"]
    assert ours["toolCallId"] == "call_Kq3vX9" and "4471" in ours["result"]
    assert theirs == {
        "toolCallId": "call_Zp2mW4",
        "name": "book_appointment",
        "error": "unknown tool book_appointment",
    }


async def test_end_of_call_report_records_the_turns_the_transfer_and_the_end(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    vapi = server(on_mock_async)
    assert (await vapi.handle(payload("end_of_call_report"), AUTH)).status == 200
    assert turns(mock_app.cell, CALL) == [
        ("ai_agent", "Hi Marina, is this about the lid of order 4471?"),
        ("customer", "Yes, it still has not arrived."),
        ("ai_agent", "I will transfer you to logistics."),
    ]
    recorded = [e for e in events(mock_app.cell, CALL) if e.kind.value == "message"]
    assert [e.occurred_at.timestamp() for e in recorded] == [1790301601, 1790301605, 1790301611]
    assert recorded[0].context_stamp is not None and recorded[0].context_stamp.etag == "etag-9"
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["human"]
    assert CALL in mock_app.cell.ended
    await vapi.handle(payload("end_of_call_report"), AUTH)
    assert len(turns(mock_app.cell, CALL)) == 3, "a redelivered report records nothing twice"


async def test_a_transfer_request_is_recorded_and_answered(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    destination = {"type": "number", "number": "+551130002000"}
    vapi = server(on_mock_async, destination=lambda _message: destination)
    answer = await vapi.handle(payload("transfer_destination_request"), AUTH)
    assert answer.body == {"destination": destination}
    await on_mock_async.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["human"]


async def test_requests_without_the_server_secret_are_refused(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    vapi = server(on_mock_async, assistant_id="asst_1")
    assert (await vapi.handle(payload("tool_calls"), {})).status == 401
    assert (await vapi.handle(payload("tool_calls"), {"x-vapi-secret": "guess"})).status == 401
    assert (await VapiServer(on_mock_async, secret=None).handle(payload("tool_calls"), AUTH)).status == 401
    assert (await vapi.handle(b"[]", AUTH)).status == 400
    assert (await vapi.handle(json.dumps({"message": {"type": "status-update"}}), AUTH)).status == 200
    assert not mock_app.cell.items


async def test_niadra_down_never_fails_the_call(on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    vapi = server(on_mock_async, assistant_id="asst_1")
    answer = await vapi.handle(payload("assistant_request"), AUTH)
    assert (
        answer.status == 200 and answer.body["assistantOverrides"]["variableValues"]["niadra_context"] == ""
    )
    tools = await vapi.handle(payload("tool_calls"), AUTH)
    assert "unavailable" in tools.body["results"][0]["result"]
    assert (await vapi.handle(payload("end_of_call_report"), AUTH)).status == 200


def test_the_sync_client(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    vapi = server(on_mock, assistant_id="asst_1")
    answer = vapi.handle_sync(payload("assistant_request"), AUTH)
    assert EARLIER in answer.body["assistantOverrides"]["variableValues"]["niadra_context"]
    assert vapi.handle_sync(payload("end_of_call_report"), AUTH).status == 200
    assert CALL in mock_app.cell.ended
    with pytest.raises(TypeError):
        server(AsyncNiadra()).handle_sync(payload("tool_calls"), AUTH)


async def test_the_agents_notes_come_first_and_the_memory_tools_answer(on_mock_async: AsyncNiadra) -> None:
    await seed_async(on_mock_async)
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    assistant = {"model": {"messages": [{"role": "system", "content": "You are Acme's agent."}]}}
    vapi = server(on_mock_async, assistant=assistant, agent_memory=True)
    answer = await vapi.handle(payload("assistant_request"), AUTH)
    slot = answer.body["assistant"]["model"]["messages"][1]["content"]
    assert slot.index("replacement order") < slot.index(EARLIER), "notes first, then the customer"
    assert "replacement order" in answer.body["assistantOverrides"]["variableValues"]["niadra_agent_memory"]
    assert [t["function"]["name"] for t in tool_definitions("u", agent_memory=True)][
        -1
    ] == "search_agent_memory"
    calls = {
        "message": {
            "type": "tool-calls",
            "toolCallList": [
                {"id": "t1", "function": {"name": "search_agent_memory", "arguments": {"query": "refund"}}}
            ],
            "call": {"id": CALL, "customer": {"number": "+5511912345678"}},
        }
    }
    results = (await vapi.handle(json.dumps(calls), AUTH)).body["results"]
    assert "Replacement parts" in results[0]["result"]
