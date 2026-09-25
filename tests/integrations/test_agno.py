"""Agno: a real `Agent` with the dynamic instructions, tools and hooks, a scripted `Model`, and the
emulator behind."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

pytest.importorskip("agno")

from agno.agent import Agent
from agno.models.base import Model
from agno.models.response import ModelResponse

from niadra import AsyncNiadra
from niadra.integrations.agno import NiadraAgno
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed_async, turns


class FakeModel(Model):
    def __init__(self, *replies: Any) -> None:
        super().__init__(id="fake-model", name="Fake", provider="Fake")
        self.replies = list(replies)
        self.prompts: list[list[Any]] = []
        self.offered: list[Any] = []

    def _next(self, messages: Any, tools: Any) -> ModelResponse:
        self.prompts.append(list(messages))
        self.offered.append(list(tools or []))
        reply = self.replies.pop(0) if self.replies else "ok"
        if isinstance(reply, tuple):
            name, arguments = reply
            call = {
                "id": "call-1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            return ModelResponse(role="assistant", tool_calls=[call])
        return ModelResponse(role="assistant", content=reply)

    def invoke(self, *args: Any, messages: Any = None, tools: Any = None, **kwargs: Any) -> ModelResponse:
        return self._next(messages, tools)

    async def ainvoke(
        self, *args: Any, messages: Any = None, tools: Any = None, **kwargs: Any
    ) -> ModelResponse:
        return self._next(messages, tools)

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:  # pragma: no cover
        yield self.invoke(*args, **kwargs)

    async def ainvoke_stream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:  # pragma: no cover
        yield await self.ainvoke(*args, **kwargs)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:  # pragma: no cover
        return response  # type: ignore[no-any-return]

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:  # pragma: no cover
        return response  # type: ignore[no-any-return]


def agent_for(memory: NiadraAgno, model: FakeModel) -> Agent:
    return Agent(
        model=model,
        instructions=memory.instructions,
        tools=memory.tools,
        pre_hooks=[memory.pre_hook],
        post_hooks=[memory.post_hook],
        telemetry=False,
    )


def system_text(model: FakeModel, index: int = 0) -> str:
    return "\n".join(str(m.content) for m in model.prompts[index] if m.role == "system")


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("agno-1", subject=MARINA)


async def test_the_pack_follows_the_instructions_and_turns_are_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeModel("Your new lid ships today.")
    memory = NiadraAgno(chat, instructions="You are Acme's agent.")
    output = await agent_for(memory, model).arun("About my lid")
    assert output.content == "Your new lid ships today."
    await on_mock_async.flush()
    system = system_text(model)
    assert system.index("You are Acme's agent.") < system.index(EARLIER)
    assert turns(mock_app.cell, "agno-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]


async def test_the_history_tools_are_the_kits_and_run_for_the_customer(chat: Any) -> None:
    memory = NiadraAgno(chat)
    for function in memory.tools:
        definition = DEFINITIONS[function.name]
        assert (function.description, function.parameters) == (
            definition["description"],
            definition["parameters"],
        )
    model = FakeModel(("search_customer_history", {"query": "lid"}), "It was 4471.")
    await agent_for(memory, model).arun("What did I report?")
    results = [m for m in model.prompts[1] if m.role == "tool"]
    assert "4471" in str(results[0].content)


async def test_the_agents_notes_and_transfers(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    memory = NiadraAgno(chat, instructions="x", agent_memory={"write": True})
    assert [f.name for f in memory.tools][-2:] == ["search_agent_memory", "remember"]
    model = FakeModel("Opening it.")
    await agent_for(memory, model).arun("Hi")
    system = system_text(model)
    assert system.index("replacement order") < system.index(EARLIER)
    memory.transferred_to_agent("billing")
    memory.transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


async def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    model = FakeModel("Still here.")
    output = await agent_for(NiadraAgno(chat, instructions="x"), model).arun("Hi")
    assert output.content == "Still here."
    assert EARLIER not in system_text(model)
