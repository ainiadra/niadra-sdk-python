"""Haystack: real pipelines and a real `Agent` with the components, hooks and tools, a scripted chat
generator, and the emulator behind."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("haystack")

from haystack import Pipeline, component
from haystack.components.agents import Agent
from haystack.dataclasses import ChatMessage, ChatRole, ToolCall

from niadra import AsyncNiadra, Niadra
from niadra.integrations.haystack import NiadraAgentHooks, NiadraContext, NiadraReply, history_tools
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, seed, seed_async, turns

INSTRUCTIONS = "You are Acme's agent."
USAGE = {"prompt_tokens": 1200, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 1024}}


@component
class FakeGenerator:
    """A chat generator that answers from a script and keeps every prompt and tool list it got."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.prompts: list[list[ChatMessage]] = []
        self.offered: list[list[Any]] = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None) -> dict[str, Any]:
        self.prompts.append(list(messages))
        self.offered.append(list(tools or []))
        reply = self.replies.pop(0) if self.replies else "ok"
        if isinstance(reply, tuple):
            name, arguments = reply
            call = ToolCall(tool_name=name, arguments=arguments, id="call-1")
            return {"replies": [ChatMessage.from_assistant(tool_calls=[call])]}
        return {"replies": [ChatMessage.from_assistant(reply, meta={"model": "gpt-4.1", "usage": USAGE})]}

    @component.output_types(replies=list[ChatMessage])
    async def run_async(self, messages: list[ChatMessage], tools: Any = None) -> dict[str, Any]:
        answered: dict[str, Any] = self.run(messages, tools)
        return answered


def pipeline_for(conversation: Any, generator: FakeGenerator) -> Pipeline:
    pipeline = Pipeline()
    pipeline.add_component("niadra", NiadraContext(conversation))
    pipeline.add_component("llm", generator)
    pipeline.add_component("reply", NiadraReply(conversation))
    pipeline.connect("niadra.messages", "llm.messages")
    pipeline.connect("llm.replies", "reply.replies")
    return pipeline


def agent_for(conversation: Any, generator: FakeGenerator, **options: Any) -> Agent:
    return Agent(
        chat_generator=generator,
        system_prompt=INSTRUCTIONS,
        tools=history_tools(conversation, options.get("agent_memory")),
        hooks=NiadraAgentHooks(conversation, **options).hooks,
    )


def ask(text: str) -> list[ChatMessage]:
    return [ChatMessage.from_system(INSTRUCTIONS), ChatMessage.from_user(text)]


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("hs-1", subject=MARINA)


async def test_a_pipeline_places_the_pack_after_the_system_message_and_records_turns(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    generator = FakeGenerator("Your new lid ships today.")
    result = await pipeline_for(chat, generator).run_async({"niadra": {"messages": ask("About my lid")}})
    assert result["reply"]["replies"][0].text == "Your new lid ships today."
    prompt = generator.prompts[0]
    assert prompt[0].text == INSTRUCTIONS
    assert prompt[1].is_from(ChatRole.SYSTEM) and EARLIER in str(prompt[1].text)
    assert prompt[2].text == "About my lid"
    await on_mock_async.flush()
    assert turns(mock_app.cell, "hs-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]
    reply = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert reply.usage is not None and (reply.usage.model, reply.usage.cached_tokens) == ("gpt-4.1", 1024)


async def test_messages_passed_back_in_never_hold_the_pack_twice(chat: Any) -> None:
    generator = FakeGenerator("One.", "Two.")
    pipeline = pipeline_for(chat, generator)
    await pipeline.run_async({"niadra": {"messages": ask("About my lid")}})
    history = [*generator.prompts[0], ChatMessage.from_assistant("One."), ChatMessage.from_user("And?")]
    await pipeline.run_async({"niadra": {"messages": history}})
    assert sum(EARLIER in str(m.text) for m in generator.prompts[1]) == 1


async def test_the_agent_hooks_and_the_kits_tools(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    tools = history_tools(chat)
    for tool in tools:
        definition = DEFINITIONS[tool.name]
        assert (tool.description, tool.parameters) == (definition["description"], definition["parameters"])
    generator = FakeGenerator(("search_customer_history", {"query": "lid"}), "It was 4471.")
    agent = agent_for(chat, generator)
    result = await agent.run_async(messages=[ChatMessage.from_user("What did I report?")])
    assert result["last_message"].text == "It was 4471."
    first = generator.prompts[0]
    assert first[0].text == INSTRUCTIONS and EARLIER in str(first[1].text)
    results = [m for m in generator.prompts[1] if m.is_from(ChatRole.TOOL)]
    assert "4471" in str(results[0].tool_call_result.result)
    assert sum(EARLIER in str(m.text) for m in generator.prompts[1]) == 1, "placed once per model call"
    assert not any(EARLIER in str(m.text) for m in result["messages"]), "the output holds no Niadra message"
    await on_mock_async.flush()
    assert turns(mock_app.cell, "hs-1") == [("customer", "What did I report?"), ("ai_agent", "It was 4471.")]


async def test_the_agents_notes_come_first(chat: Any, on_mock_async: AsyncNiadra) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    assert [t.name for t in history_tools(chat, {"write": True})][-2:] == ["search_agent_memory", "remember"]
    generator = FakeGenerator("Opening it.")
    await agent_for(chat, generator, agent_memory=True).run_async(messages=[ChatMessage.from_user("Hi")])
    slot = str(generator.prompts[0][1].text)
    assert slot.index("replacement order") < slot.index(EARLIER)


async def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    generator = FakeGenerator(("search_customer_history", {"query": "lid"}), "Still here.")
    result = await agent_for(chat, generator).run_async(messages=[ChatMessage.from_user("Hi")])
    assert result["last_message"].text == "Still here."
    assert not any(EARLIER in str(m.text) for m in generator.prompts[0])
    results = [m for m in generator.prompts[1] if m.is_from(ChatRole.TOOL)]
    assert "unavailable" in str(results[0].tool_call_result.result)


def test_the_sync_client_with_a_sync_pipeline_and_agent(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    conversation = on_mock.conversation("hs-sync", subject=MARINA)
    generator = FakeGenerator("Done.", ("search_customer_history", {"query": "lid"}), "4471.")
    pipeline_for(conversation, generator).run({"niadra": {"messages": ask("About my lid")}})
    assert EARLIER in str(generator.prompts[0][1].text)
    result = agent_for(conversation, generator).run(messages=[ChatMessage.from_user("What did I report?")])
    assert result["last_message"].text == "4471."
    results = [m for m in generator.prompts[2] if m.is_from(ChatRole.TOOL)]
    assert "4471" in str(results[0].tool_call_result.result)
    on_mock.flush()
    assert turns(mock_app.cell, "hs-sync") == [
        ("customer", "About my lid"),
        ("ai_agent", "Done."),
        ("customer", "What did I report?"),
        ("ai_agent", "4471."),
    ]
