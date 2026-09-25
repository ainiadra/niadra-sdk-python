"""Microsoft Agent Framework: a real `Agent` with the context provider, a scripted chat client with
the framework's own function invocation layer, and the emulator behind."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("agent_framework")

from agent_framework import Agent, BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer

from niadra import AsyncNiadra
from niadra.integrations.agent_framework import NiadraContextProvider
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed_async, turns

USAGE = {"input_token_count": 1400, "cache_read_input_token_count": 1024, "output_token_count": 9}


class FakeClient(FunctionInvocationLayer, BaseChatClient):  # type: ignore[misc]
    def __init__(self, *replies: Any) -> None:
        super().__init__()
        self.replies = list(replies)
        self.prompts: list[list[Any]] = []
        self.options: list[Any] = []

    async def _inner_get_response(self, *, messages: Any, stream: bool, options: Any, **kwargs: Any) -> Any:
        self.prompts.append(list(messages))
        self.options.append(dict(options))
        reply = self.replies.pop(0) if self.replies else "ok"
        if isinstance(reply, tuple):
            name, arguments = reply
            call = Content.from_function_call(call_id="call-1", name=name, arguments=arguments)
            return ChatResponse(messages=[Message(role="assistant", contents=[call])], model="gpt-4.1")
        message = Message(role="assistant", contents=[reply])
        return ChatResponse(messages=[message], usage_details=USAGE, model="gpt-4.1")


def instructions(client: FakeClient, index: int = 0) -> str:
    system = [m.text for m in client.prompts[index] if str(m.role) == "system"]
    return "\n".join(system) or str(client.options[index].get("instructions") or "")


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("maf-1", subject=MARINA)


async def test_the_pack_follows_the_instructions_and_turns_are_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    client = FakeClient("Your new lid ships today.")
    agent = Agent(
        client=client, instructions="You are Acme's agent.", context_providers=[NiadraContextProvider(chat)]
    )
    response = await agent.run("About my lid")
    assert response.text == "Your new lid ships today."
    await on_mock_async.flush()
    text = instructions(client)
    assert text.index("You are Acme's agent.") < text.index(EARLIER)
    assert turns(mock_app.cell, "maf-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None and answer.usage.cached_tokens == 1024


async def test_the_history_tools_are_the_kits_and_run_for_the_customer(chat: Any) -> None:
    provider = NiadraContextProvider(chat)
    for tool in provider.tools:
        definition = DEFINITIONS[tool.name]
        assert tool.description == definition["description"]
        assert tool.parameters() == definition["parameters"]
    client = FakeClient(("search_customer_history", {"query": "lid"}), "It was 4471.")
    await Agent(client=client, context_providers=[provider]).run("What did I report?")
    results = [c for m in client.prompts[1] for c in m.contents if c.type == "function_result"]
    assert "4471" in str(results[0].result)


async def test_the_agents_notes_and_transfers(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    provider = NiadraContextProvider(chat, agent_memory={"write": True})
    assert [t.name for t in provider.tools][-2:] == ["search_agent_memory", "remember"]
    client = FakeClient("Opening it.")
    await Agent(client=client, instructions="x", context_providers=[provider]).run("Hi")
    text = instructions(client)
    assert text.index("replacement order") < text.index(EARLIER)
    provider.transferred_to_agent("billing")
    provider.transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


async def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    client = FakeClient("Still here.")
    response = await Agent(
        client=client, instructions="x", context_providers=[NiadraContextProvider(chat)]
    ).run("Hi")
    assert response.text == "Still here."
    assert EARLIER not in instructions(client)
