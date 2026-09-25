"""AG2: a real `Agent` with the middleware and the tools, AG2's own scripted test client, and the
emulator behind."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("ag2")

from ag2 import Agent
from ag2.events import ModelMessage, ModelResponse, ToolCallEvent, ToolResultEvent, Usage
from ag2.testing import TestClient, TestConfig

from niadra import AsyncNiadra, Niadra
from niadra.integrations.ag2 import NiadraAG2
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed, seed_async, turns

INSTRUCTIONS = "You are Acme's agent."


class Recording(TestClient):
    """AG2's scripted client, keeping the prompt, the history and the tools of every call."""

    def __init__(self, calls: list[dict[str, Any]], *events: Any) -> None:
        super().__init__(*events)
        self.calls = calls

    async def __call__(self, messages: Sequence[Any], context: Any, **kwargs: Any) -> ModelResponse:
        self.calls.append(
            {
                "prompt": list(context.prompt),
                "messages": list(messages),
                "tools": list(kwargs.get("tools") or []),
            }
        )
        return await super().__call__(messages, context, **kwargs)


class Script(TestConfig):
    def __init__(self, *events: Any) -> None:
        super().__init__(*events)
        self.calls: list[dict[str, Any]] = []

    def create(self) -> TestClient:
        return Recording(self.calls, *self.events)


def answer(text: str) -> ModelResponse:
    usage = Usage(prompt_tokens=1200, completion_tokens=7, cache_read_input_tokens=1024)
    return ModelResponse(ModelMessage(text), usage=usage, model="gpt-4.1", provider="openai")


def result_text(messages: list[Any]) -> str:
    """The text of the first tool result the model was given back."""
    result = next(m for m in messages if isinstance(m, ToolResultEvent)).result
    return " ".join(str(getattr(part, "content", part)) for part in result.parts)


def call(name: str, arguments: dict[str, Any]) -> ToolCallEvent:
    return ToolCallEvent(name, arguments=json.dumps(arguments))


def agent_for(memory: NiadraAG2, script: Script) -> Agent:
    return Agent("acme", INSTRUCTIONS, config=script, tools=memory.tools, middleware=[memory.middleware])


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("ag2-1", subject=MARINA)


async def test_the_pack_follows_the_instructions_and_turns_are_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    script = Script(answer("Your new lid ships today."))
    reply = await agent_for(NiadraAG2(chat), script).ask("About my lid")
    assert reply.body == "Your new lid ships today."
    prompt = script.calls[0]["prompt"]
    assert prompt[0] == INSTRUCTIONS and EARLIER in prompt[1]
    await on_mock_async.flush()
    assert turns(mock_app.cell, "ag2-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]
    recorded = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert recorded.usage is not None
    assert (recorded.usage.provider, recorded.usage.cached_tokens) == ("openai", 1024)


async def test_the_history_tools_are_the_kits_and_run_for_the_customer(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    memory = NiadraAG2(chat)
    script = Script(call("search_customer_history", {"query": "lid"}), answer("It was 4471."))
    reply = await agent_for(memory, script).ask("What did I report?")
    assert reply.body == "It was 4471."
    offered = {t.function.name: t.function for t in script.calls[0]["tools"]}
    assert set(offered) == set(DEFINITIONS)
    for name, definition in DEFINITIONS.items():
        assert (offered[name].description, offered[name].parameters) == (
            definition["description"],
            definition["parameters"],
        )
    assert "4471" in result_text(script.calls[1]["messages"])
    assert len(script.calls[1]["prompt"]) == 2, "the fragment is placed once per call, never piled up"
    await on_mock_async.flush()
    assert turns(mock_app.cell, "ag2-1") == [("customer", "What did I report?"), ("ai_agent", "It was 4471.")]


async def test_the_agents_notes_and_transfers(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    memory = NiadraAG2(chat, agent_memory={"write": True})
    assert [t.name for t in memory.tools][-2:] == ["search_agent_memory", "remember"]
    script = Script(answer("Opening it."))
    await agent_for(memory, script).ask("Hi")
    fragment = script.calls[0]["prompt"][1]
    assert fragment.index("replacement order") < fragment.index(EARLIER)
    memory.transferred_to_agent("billing")
    memory.transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


async def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    script = Script(call("search_customer_history", {"query": "lid"}), answer("Still here."))
    reply = await agent_for(NiadraAG2(chat), script).ask("Hi")
    assert reply.body == "Still here."
    assert script.calls[0]["prompt"] == [INSTRUCTIONS]
    assert "unavailable" in result_text(script.calls[1]["messages"])


async def test_the_sync_client(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    memory = NiadraAG2(on_mock.conversation("ag2-sync", subject=MARINA))
    script = Script(call("search_customer_history", {"query": "lid"}), answer("4471."))
    await agent_for(memory, script).ask("About my lid")
    assert EARLIER in script.calls[0]["prompt"][1]
    assert "4471" in result_text(script.calls[1]["messages"])
    on_mock.flush()
    assert turns(mock_app.cell, "ag2-sync") == [("customer", "About my lid"), ("ai_agent", "4471.")]
