"""OpenAI Agents SDK: real `Agent`, `Runner`, `RunConfig` and hooks, the SDK's own scripted model,
and the emulator behind."""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("agents")

from agents import Agent, RunConfig, Runner, SQLiteSession
from agents.run_config import ModelInputData
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.usage import Usage
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from niadra import AsyncNiadra
from niadra.integrations.openai_agents import NiadraAgentsMemory, history_tools
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed_async, turns


def usage(prompt: int, cached: int) -> Usage:
    return Usage(
        requests=1,
        input_tokens=prompt,
        input_tokens_details=InputTokensDetails(cached_tokens=cached, cache_write_tokens=0),
        output_tokens=9,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=prompt + 9,
    )


def said(text: str, response_id: str = "resp-1") -> ModelStep:
    return ModelStep(output=[assistant_message(text)], usage=usage(1400, 1024), response_id=response_id)


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("thread-1", subject=MARINA, channel="chat")


async def test_the_pack_follows_the_instructions_and_turns_are_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = ScriptedModel([said("Your new lid ships today.")])
    model.model = "gpt-4.1"  # type: ignore[attr-defined]  # as the SDK's OpenAI models name themselves
    memory = NiadraAgentsMemory(chat)
    agent = Agent(name="Support", instructions="You are Acme's agent.", model=model, tools=memory.tools)
    result = await Runner.run(agent, "Hi, about my lid", hooks=memory.hooks, run_config=memory.run_config())
    assert result.final_output == "Your new lid ships today."
    await on_mock_async.flush()

    (call,) = model.calls
    assert call.system_instructions is not None
    assert call.system_instructions.startswith("You are Acme's agent.\n\n<context")
    assert EARLIER in call.system_instructions
    assert turns(mock_app.cell, "thread-1") == [
        ("customer", "Hi, about my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None
    assert (answer.usage.model, answer.usage.prompt_tokens, answer.usage.cached_tokens) == (
        "gpt-4.1",
        1400,
        1024,
    )
    assert answer.context_stamp is not None


async def test_news_from_another_channel_goes_after_the_input(chat: Any, on_mock_async: AsyncNiadra) -> None:
    await chat.context()
    on_mock_async.track(
        {
            "channel": "voice",
            "conversation_id": "call-9",
            "handles": [MARINA],
            "speaker": {"role": "customer"},
            "content": {"text": "I called about the lid too"},
        }
    )
    await on_mock_async.flush()
    model = ScriptedModel([said("Got it.")])
    memory = NiadraAgentsMemory(chat)
    await Runner.run(Agent(name="S", instructions="x", model=model), "Hi", run_config=memory.run_config())
    last = model.calls[0].input[-1]
    assert last["role"] == "system" and "I called about the lid" in last["content"]


async def test_history_tools_are_the_kits_and_bound_to_the_customer(
    chat: Any, on_mock_async: AsyncNiadra
) -> None:
    tools = history_tools(chat)
    assert {
        t.name: {"name": t.name, "description": t.description, "parameters": t.params_json_schema}
        for t in tools
    } == DEFINITIONS
    model = ScriptedModel(
        [
            ModelStep(output=[function_call("search_customer_history", {"query": "lid"}, call_id="c1")]),
            said("It was the lid of order 4471."),
        ]
    )
    memory = NiadraAgentsMemory(chat)
    agent = Agent(name="Support", instructions="x", model=model, tools=memory.tools)
    await Runner.run(agent, "What did I report?", hooks=memory.hooks, run_config=memory.run_config())
    output = next(
        i for i in model.calls[1].input if isinstance(i, dict) and i.get("type") == "function_call_output"
    )
    assert "4471" in output["output"]


async def test_a_session_replaying_history_records_nothing_twice(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    session = SQLiteSession("thread-1")
    memory = NiadraAgentsMemory(chat)
    model = ScriptedModel([said("First answer.", "resp-1"), said("Second answer.", "resp-2")])
    agent = Agent(name="Support", instructions="x", model=model)
    for text in ("First question", "Second question"):
        await Runner.run(agent, text, session=session, hooks=memory.hooks, run_config=memory.run_config())
    await on_mock_async.flush()
    assert turns(mock_app.cell, "thread-1") == [
        ("customer", "First question"),
        ("ai_agent", "First answer."),
        ("customer", "Second question"),
        ("ai_agent", "Second answer."),
    ]


async def test_a_handoff_is_recorded(chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    billing_model = ScriptedModel([said("Billing here.")])
    billing = Agent(name="Billing", instructions="You handle billing.", model=billing_model)
    model = ScriptedModel([ModelStep(output=[function_call("transfer_to_billing", {}, call_id="h1")])])
    triage = Agent(name="Triage", instructions="Route the customer.", model=model, handoffs=[billing])
    memory = NiadraAgentsMemory(chat)
    await Runner.run(triage, "My invoice", hooks=memory.hooks, run_config=memory.run_config())
    await on_mock_async.flush()
    assert [(h.target, h.reason) for h in items(mock_app.cell, "handoff")] == [("agent", "Triage to Billing")]
    assert EARLIER in (billing_model.calls[0].system_instructions or ""), (
        "the next agent reads the context too"
    )


async def test_a_filter_you_had_runs_first(chat: Any) -> None:
    seen: list[str] = []

    def yours(data: Any) -> ModelInputData:
        seen.append("yours")
        return ModelInputData(input=data.model_data.input, instructions="Yours.")

    model = ScriptedModel([said("ok")])
    config = NiadraAgentsMemory(chat).run_config(RunConfig(call_model_input_filter=yours))
    await Runner.run(Agent(name="S", instructions="x", model=model), "Hi", run_config=config)
    assert seen == ["yours"]
    assert (model.calls[0].system_instructions or "").startswith("Yours.\n\n<context")


async def test_niadra_down_never_stops_the_run(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    model = ScriptedModel(
        [
            ModelStep(output=[function_call("search_customer_history", {"query": "lid"}, call_id="c1")]),
            said("ok"),
        ]
    )
    memory = NiadraAgentsMemory(chat)
    agent = Agent(name="S", instructions="x", model=model, tools=memory.tools)
    result = await Runner.run(agent, "Hi", hooks=memory.hooks, run_config=memory.run_config())
    assert result.final_output == "ok"
    assert model.calls[0].system_instructions == "x"
    output = next(
        i for i in model.calls[1].input if isinstance(i, dict) and i.get("type") == "function_call_output"
    )
    assert "unavailable" in json.loads(output["output"])["error"]
