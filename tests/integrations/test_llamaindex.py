"""LlamaIndex: a real `FunctionAgent` with the memory and the tools, LlamaIndex's own
`MockFunctionCallingLLM` driven by a script, and the emulator behind."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("llama_index.core")

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.base.llms.types import ChatMessage, MessageRole, ToolCallBlock
from llama_index.core.llms.mock import MockFunctionCallingLLM

from niadra import AsyncNiadra
from niadra.integrations.llamaindex import NiadraMemory, history_tools
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, seed_async, turns


class Script:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.prompts: list[list[Any]] = []
        self.tools: list[Any] = []

    def __call__(self, messages: Any, **kwargs: Any) -> Any:
        self.prompts.append(list(messages))
        self.tools.append(list(kwargs.get("tools") or []))
        reply = self.replies.pop(0) if self.replies else "ok"
        if isinstance(reply, tuple):
            name, arguments = reply
            block = ToolCallBlock(tool_call_id="call-1", tool_name=name, tool_kwargs=arguments)
            return ChatMessage(role=MessageRole.ASSISTANT, blocks=[block])
        return ChatMessage(role=MessageRole.ASSISTANT, content=reply)


def agent(script: Script, tools: list[Any]) -> FunctionAgent:
    llm = MockFunctionCallingLLM(response_generator=script, is_chat_model=True)
    return FunctionAgent(llm=llm, system_prompt="You are Acme's agent.", tools=tools)


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("li-1", subject=MARINA)


async def test_the_pack_follows_the_system_prompt_and_turns_are_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    script = Script("Your new lid ships today.")
    memory = NiadraMemory(chat)
    await agent(script, []).run("About my lid", memory=memory)
    await on_mock_async.flush()
    prompt = script.prompts[0]
    assert prompt[0].content == "You are Acme's agent."
    assert prompt[1].role == MessageRole.SYSTEM and EARLIER in str(prompt[1].content)
    assert not any(EARLIER in str(m.content) for m in memory.get_all()), "only the history is stored"
    assert turns(mock_app.cell, "li-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]


async def test_the_history_tools_are_the_kits_and_run_for_the_customer(chat: Any) -> None:
    tools = history_tools(chat)
    assert {t.metadata.name: t.metadata.get_parameters_dict() for t in tools} == {
        n: d["parameters"] for n, d in DEFINITIONS.items()
    }
    assert {t.metadata.name: t.metadata.description for t in tools} == {
        n: d["description"] for n, d in DEFINITIONS.items()
    }
    script = Script(("search_customer_history", {"query": "lid"}), "It was 4471.")
    await agent(script, tools).run("What did I report?", memory=NiadraMemory(chat))
    results = [m for m in script.prompts[1] if m.role == MessageRole.TOOL]
    assert "4471" in str(results[0].content)


async def test_the_agents_notes_come_first(chat: Any, on_mock_async: AsyncNiadra) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    script = Script("Opening it.")
    tools = history_tools(chat, agent_memory={"write": True})
    assert [t.metadata.name for t in tools][-2:] == ["search_agent_memory", "remember"]
    await agent(script, tools).run("Hi", memory=NiadraMemory(chat, agent_memory=True))
    slot = str(script.prompts[0][1].content)
    assert slot.index("replacement order") < slot.index(EARLIER)


async def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    script = Script("Still here.")
    await agent(script, []).run("Hi", memory=NiadraMemory(chat))
    assert [m.role for m in script.prompts[0]] == [MessageRole.SYSTEM, MessageRole.USER]
