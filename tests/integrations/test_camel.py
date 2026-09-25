"""CAMEL-AI: a real `ChatAgent` with the memory and the tools, a scripted model backend, and the
emulator behind."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

pytest.importorskip("camel")

from camel.agents import ChatAgent
from camel.models.stub_model import StubModel
from camel.types import ModelType
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function
from openai.types.completion_usage import CompletionUsage

from niadra import AsyncNiadra, Niadra
from niadra.integrations.camel import NiadraMemory, NiadraToolkit, history_tools
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed, turns

INSTRUCTIONS = "You are Acme's agent."


class FakeModel(StubModel):
    """A model backend that answers from a script and keeps every prompt and tool list it got."""

    def __init__(self, *replies: Any) -> None:
        super().__init__(ModelType.STUB)
        self.replies = list(replies)
        self.prompts: list[list[dict[str, Any]]] = []
        self.offered: list[list[dict[str, Any]]] = []

    def _next(self, messages: Any, tools: Any) -> ChatCompletion:
        self.prompts.append([dict(m) for m in messages])
        self.offered.append(list(tools or []))
        reply = self.replies.pop(0) if self.replies else "ok"
        if isinstance(reply, tuple):
            name, arguments = reply
            call = ChatCompletionMessageToolCall(
                id="call-1", type="function", function=Function(name=name, arguments=json.dumps(arguments))
            )
            message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[call])
            finish = "tool_calls"
        else:
            message = ChatCompletionMessage(role="assistant", content=reply)
            finish = "stop"
        return ChatCompletion(
            id="fake",
            model="gpt-4.1",
            object="chat.completion",
            created=int(time.time()),
            choices=[Choice(finish_reason=finish, index=0, message=message)],
            usage=CompletionUsage(prompt_tokens=1200, completion_tokens=7, total_tokens=1207),
        )

    def _run(self, messages: Any, response_format: Any = None, tools: Any = None) -> Any:
        return self._next(messages, tools)

    async def _arun(self, messages: Any, response_format: Any = None, tools: Any = None) -> Any:
        return self._next(messages, tools)


def agent_for(memory: NiadraMemory, model: FakeModel) -> ChatAgent:
    agent: ChatAgent = memory.attach(ChatAgent(system_message=INSTRUCTIONS, model=model, tools=memory.tools))
    return agent


@pytest.fixture
def chat(on_mock: Niadra) -> Any:
    seed(on_mock)
    return on_mock.conversation("camel-1", subject=MARINA)


def test_the_pack_follows_the_system_message_and_turns_are_recorded(
    chat: Any, on_mock: Niadra, mock_app: MockApp
) -> None:
    model = FakeModel("Your new lid ships today.")
    memory = NiadraMemory(chat)
    response = agent_for(memory, model).step("About my lid")
    assert response.msgs[0].content == "Your new lid ships today."
    prompt = model.prompts[0]
    assert prompt[0] == {"role": "system", "content": INSTRUCTIONS}
    assert prompt[1]["role"] == "system" and EARLIER in prompt[1]["content"]
    assert prompt[2]["content"] == "About my lid"
    stored = [r.memory_record.message.content for r in memory.retrieve()]
    assert stored == [INSTRUCTIONS, "About my lid", "Your new lid ships today."], "only the chat is stored"
    on_mock.flush()
    assert turns(mock_app.cell, "camel-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]


def test_the_history_tools_are_the_kits_and_run_for_the_customer(
    chat: Any, on_mock: Niadra, mock_app: MockApp
) -> None:
    memory = NiadraMemory(chat)
    for tool in memory.tools:
        schema = tool.get_openai_tool_schema()
        assert schema["function"] == DEFINITIONS[schema["function"]["name"]]
    assert [t.get_function_name() for t in NiadraToolkit(chat).get_tools()] == list(DEFINITIONS)
    model = FakeModel(("search_customer_history", {"query": "lid"}), "It was 4471.")
    response = agent_for(memory, model).step("What did I report?")
    assert response.msgs[0].content == "It was 4471."
    offered = [t["function"]["name"] for t in model.offered[0]]
    assert offered == list(DEFINITIONS)
    results = [m for m in model.prompts[1] if m["role"] == "tool"]
    assert "4471" in results[0]["content"]
    on_mock.flush()
    assert turns(mock_app.cell, "camel-1")[-1] == ("ai_agent", "It was 4471.")


def test_the_agents_notes_and_transfers(chat: Any, on_mock: Niadra, mock_app: MockApp) -> None:
    on_mock.remember("procedure", "Replacement parts", "Open a replacement order before any refund.")
    memory = NiadraMemory(chat, agent_memory={"write": True})
    assert [t.get_function_name() for t in memory.tools][-2:] == ["search_agent_memory", "remember"]
    model = FakeModel("Opening it.")
    agent_for(memory, model).step("Hi")
    slot = model.prompts[0][1]["content"]
    assert slot.index("replacement order") < slot.index(EARLIER)
    memory.transferred_to_agent("billing")
    memory.transferred_to_human("asked for a person")
    on_mock.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    model = FakeModel(("search_customer_history", {"query": "lid"}), "Still here.")
    response = agent_for(NiadraMemory(chat), model).step("Hi")
    assert response.msgs[0].content == "Still here."
    assert not any(EARLIER in str(m.get("content")) for m in model.prompts[0])
    results = [m for m in model.prompts[1] if m["role"] == "tool"]
    assert "unavailable" in results[0]["content"]


async def test_astep_with_the_async_client_runs_the_tools_and_records_turns(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    conversation = on_mock_async.conversation("camel-async", subject=MARINA)
    await on_mock_async.remember("pitfall", "Lids", "Lids ship apart from the body.")
    model = FakeModel(("search_agent_memory", {"query": "lids"}), "They ship apart.")
    memory = NiadraMemory(conversation, agent_memory=True)
    response = await agent_for(memory, model).astep("Where is my lid?")
    assert response.msgs[0].content == "They ship apart."
    results = [m for m in model.prompts[1] if m["role"] == "tool"]
    assert "Lids ship apart" in results[0]["content"]
    await on_mock_async.flush()
    assert turns(mock_app.cell, "camel-async") == [
        ("customer", "Where is my lid?"),
        ("ai_agent", "They ship apart."),
    ]


def test_history_tools_without_a_customer_are_none(on_mock: Niadra) -> None:
    assert history_tools(on_mock.conversation("camel-anon")) == []
