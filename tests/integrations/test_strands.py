"""Strands Agents: a real `Agent` with the hook provider, a scripted `Model` that streams the
Converse event shape, and the emulator behind."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("strands")

from strands import Agent
from strands.models import Model

from niadra import AsyncNiadra
from niadra.integrations.strands import NiadraHooks
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed_async, turns

USAGE = {"inputTokens": 180, "outputTokens": 9, "totalTokens": 1213, "cacheReadInputTokens": 1024}


def text(value: str) -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"text": value}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": USAGE, "metrics": {"latencyMs": 1}}},
    ]


def tool_use(name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": USAGE, "metrics": {"latencyMs": 1}}},
    ]


class FakeModel(Model):
    def __init__(self, *replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.systems: list[str] = []
        self.messages: list[list[Any]] = []

    def update_config(self, **config: Any) -> None:
        return None

    def get_config(self) -> Any:
        return {"model_id": "anthropic.claude-sonnet-4-5"}

    async def structured_output(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError
        yield

    async def stream(
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        content = kwargs.get("system_prompt_content") or []
        self.systems.append("\n".join(b.get("text", "") for b in content) or (system_prompt or ""))
        self.messages.append(list(messages))
        for event in self.replies.pop(0) if self.replies else text("ok"):
            yield event


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("strands-1", subject=MARINA)


async def test_the_pack_follows_the_system_prompt_and_turns_are_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeModel(text("Your new lid ships today."), text("You're welcome."))
    memory = NiadraHooks(chat)
    agent = Agent(
        model=model,
        system_prompt="You are Acme's agent.",
        tools=memory.tools,
        hooks=[memory],
        callback_handler=None,
    )
    await agent.invoke_async("About my lid")
    await agent.invoke_async("Thanks")
    await on_mock_async.flush()
    assert model.systems[0].startswith("You are Acme's agent.") and EARLIER in model.systems[0]
    assert agent.system_prompt == "You are Acme's agent.", "your prompt is restored after each call"
    assert turns(mock_app.cell, "strands-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
        ("customer", "Thanks"),
        ("ai_agent", "You're welcome."),
    ]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None
    assert (answer.usage.model, answer.usage.prompt_tokens, answer.usage.cached_tokens) == (
        "anthropic.claude-sonnet-4-5",
        1204,
        1024,
    )


async def test_the_history_tools_are_the_kits_and_run_for_the_customer(chat: Any) -> None:
    memory = NiadraHooks(chat)
    specs = {t.tool_name: t.tool_spec for t in memory.tools}
    assert {
        n: {"name": n, "description": s["description"], "parameters": s["inputSchema"]["json"]}
        for n, s in specs.items()
    } == DEFINITIONS
    model = FakeModel(tool_use("search_customer_history", {"query": "lid"}), text("It was 4471."))
    agent = Agent(model=model, tools=memory.tools, hooks=[memory], callback_handler=None)
    await agent.invoke_async("What did I report?")
    results = [b["toolResult"] for m in model.messages[1] for b in m["content"] if "toolResult" in b]
    assert "4471" in results[0]["content"][0]["text"]


async def test_the_agents_notes_and_transfers(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    memory = NiadraHooks(chat, agent_memory={"write": True})
    assert [t.tool_name for t in memory.tools][-2:] == ["search_agent_memory", "remember"]
    model = FakeModel(text("Opening it."))
    await Agent(model=model, system_prompt="x", hooks=[memory], callback_handler=None).invoke_async("Hi")
    assert model.systems[0].index("replacement order") < model.systems[0].index(EARLIER)
    memory.transferred_to_agent("billing")
    memory.transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


async def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    model = FakeModel(text("Still here."))
    memory = NiadraHooks(chat)
    result = await Agent(model=model, system_prompt="x", hooks=[memory], callback_handler=None).invoke_async(
        "Hi"
    )
    assert "Still here." in str(result)
    assert model.systems[0] == "x"
