"""Pydantic AI: a real `Agent` with the capability, Pydantic AI's own `FunctionModel`, and the
emulator behind."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from niadra import AsyncNiadra
from niadra.integrations.pydantic_ai import NiadraCapability
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed_async, turns


class Script:
    """The model: the next scripted part, and every request's instructions and messages kept."""

    def __init__(self, *parts: Any) -> None:
        self.parts = list(parts)
        self.instructions: list[str] = []
        self.messages: list[list[Any]] = []
        self.tools: list[Any] = []

    def __call__(self, messages: list[Any], info: AgentInfo) -> ModelResponse:
        self.instructions.append(info.instructions or "")
        self.messages.append(list(messages))
        self.tools.append(list(info.function_tools))
        part = self.parts.pop(0) if self.parts else TextPart("ok")
        usage = RequestUsage(input_tokens=1400, cache_read_tokens=1024, output_tokens=9)
        return ModelResponse(parts=[part], usage=usage, model_name="gpt-4.1")


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("pai-1", subject=MARINA)


async def test_the_pack_follows_the_instructions_and_turns_are_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    script = Script(TextPart("Your new lid ships today."))
    agent = Agent(
        FunctionModel(script), instructions="You are Acme's agent.", capabilities=[NiadraCapability(chat)]
    )
    result = await agent.run("About my lid")
    assert result.output == "Your new lid ships today."
    await on_mock_async.flush()
    instructions = script.instructions[0]
    assert instructions.index("You are Acme's agent.") < instructions.index(EARLIER)
    parts = [p for m in result.all_messages() for p in m.parts]
    assert not any(EARLIER in str(getattr(p, "content", "")) for p in parts), "no message part holds the pack"
    assert turns(mock_app.cell, "pai-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None and (answer.usage.model, answer.usage.cached_tokens) == (
        "function:Script:",
        1024,
    )
    assert answer.context_stamp is not None


async def test_the_history_tools_are_the_kits_and_run_for_the_customer(chat: Any) -> None:
    script = Script(ToolCallPart("search_customer_history", {"query": "lid"}), TextPart("It was 4471."))
    agent = Agent(FunctionModel(script), capabilities=[NiadraCapability(chat)])
    await agent.run("What did I report?")
    offered = {t.name: t for t in script.tools[0]}
    for name, definition in DEFINITIONS.items():
        assert offered[name].description == definition["description"]
        assert offered[name].parameters_json_schema == definition["parameters"]
    returned = [p for m in script.messages[1] for p in m.parts if isinstance(p, ToolReturnPart)]
    assert "4471" in str(returned[0].content)


async def test_the_agents_notes_and_transfers(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    script = Script(TextPart("Opening it."))
    capability = NiadraCapability(chat, agent_memory={"write": True})
    await Agent(FunctionModel(script), instructions="x", capabilities=[capability]).run("Hi")
    assert script.instructions[0].index("replacement order") < script.instructions[0].index(EARLIER)
    assert [t.name for t in script.tools[0]][-2:] == ["search_agent_memory", "remember"]
    capability.transferred_to_agent("billing")
    capability.transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


async def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    script = Script(TextPart("Still here."))
    result = await Agent(FunctionModel(script), instructions="x", capabilities=[NiadraCapability(chat)]).run(
        "Hi"
    )
    assert result.output == "Still here."
    assert script.instructions[0].strip() == "x"
