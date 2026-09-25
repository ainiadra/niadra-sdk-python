"""LangGraph: real `create_agent` agents with the middleware, a checkpointer, the older
`create_react_agent` with the pre-model hook, a scripted chat model, and the emulator behind."""

from __future__ import annotations

import warnings
from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain.agents")

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from niadra import AsyncNiadra
from niadra.integrations.langchain import NiadraCallbackHandler
from niadra.integrations.langgraph import NiadraMiddleware, pre_model_hook
from niadra_mock import MockApp
from tests.integrations.fake_chat import FakeChat, answer, tool_call
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed_async, turns

INSTRUCTIONS = "You are Acme's agent."


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("graph-1", subject=MARINA)


def say(text: str) -> dict[str, Any]:
    return {"messages": [HumanMessage(content=text)]}


async def test_the_middleware_puts_the_pack_in_the_system_message_and_records_turns(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeChat(replies=[answer("Your new lid ships today.")])
    agent = create_agent(model, system_prompt=INSTRUCTIONS, middleware=[NiadraMiddleware(chat)])
    result = await agent.ainvoke(say("About my lid"))
    assert result["messages"][-1].content == "Your new lid ships today."
    await on_mock_async.flush()

    (prompt,) = model.prompts
    assert isinstance(prompt[0], SystemMessage)
    system = str(prompt[0].content)
    assert system.startswith(INSTRUCTIONS + "\n\n<context") and EARLIER in system
    assert not any(EARLIER in str(m.content) for m in result["messages"]), "the state never holds the pack"
    assert [t["function"]["name"] if isinstance(t, dict) else t.name for t in model.tools] == list(
        DEFINITIONS
    )
    assert turns(mock_app.cell, "graph-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]


async def test_a_checkpointed_thread_records_each_turn_once(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeChat(replies=[answer("First.", "m1"), answer("Second.", "m2")])
    agent = create_agent(model, middleware=[NiadraMiddleware(chat)], checkpointer=InMemorySaver())
    config: Any = {"configurable": {"thread_id": "graph-1"}}
    await agent.ainvoke(say("Question one"), config)
    await agent.ainvoke(say("Question two"), config)
    await on_mock_async.flush()
    assert turns(mock_app.cell, "graph-1") == [
        ("customer", "Question one"),
        ("ai_agent", "First."),
        ("customer", "Question two"),
        ("ai_agent", "Second."),
    ]


async def test_the_history_tools_run_for_the_customer(chat: Any) -> None:
    model = FakeChat(replies=[tool_call("search_customer_history", {"query": "lid"}), answer("It was 4471.")])
    agent = create_agent(model, middleware=[NiadraMiddleware(chat)])
    result = await agent.ainvoke(say("What did I report?"))
    (tool_result,) = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert "4471" in str(tool_result.content)


async def test_the_agents_notes_and_memory_tools(chat: Any, on_mock_async: AsyncNiadra) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    model = FakeChat(replies=[answer("Opening it.")])
    middleware = NiadraMiddleware(chat, agent_memory={"write": True})
    await create_agent(model, system_prompt=INSTRUCTIONS, middleware=[middleware]).ainvoke(say("Hi"))
    system = str(model.prompts[0][0].content)
    assert system.index(INSTRUCTIONS) < system.index("replacement order") < system.index(EARLIER)
    assert [t.name for t in middleware.tools][-2:] == ["search_agent_memory", "remember"]


async def test_transfers_are_recorded(chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    middleware = NiadraMiddleware(chat)
    middleware.transferred_to_agent("billing")
    middleware.transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


async def test_niadra_down_never_stops_the_graph(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    model = FakeChat(replies=[answer("Still here.")])
    result = await create_agent(
        model, system_prompt=INSTRUCTIONS, middleware=[NiadraMiddleware(chat)]
    ).ainvoke(say("Hi"))
    assert result["messages"][-1].content == "Still here."
    assert str(model.prompts[0][0].content) == INSTRUCTIONS


async def test_the_older_react_agent_takes_the_pre_model_hook(chat: Any, on_mock_async: AsyncNiadra) -> None:
    from langgraph.prebuilt import create_react_agent

    model = FakeChat(replies=[answer("Hello.")])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        agent = create_react_agent(model, tools=[], prompt=INSTRUCTIONS, pre_model_hook=pre_model_hook(chat))
    result = await agent.ainvoke(say("Hi"), config={"callbacks": [NiadraCallbackHandler(chat)]})
    assert any(EARLIER in str(m.content) for m in model.prompts[0])
    assert not any(EARLIER in str(m.content) for m in result["messages"])
