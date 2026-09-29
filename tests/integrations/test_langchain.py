"""LangChain: real prompts, runnables, tools and callbacks from `langchain-core`, a scripted chat
model, and the emulator behind."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("langchain_core")

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate

from niadra import AsyncNiadra, Niadra
from niadra.integrations.langchain import (
    NiadraCallbackHandler,
    awith_context,
    context_runnable,
    history_tools,
    with_context,
)
from niadra_mock import MockApp
from tests.integrations.fake_chat import FakeChat, answer
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, seed, seed_async, turns

PROMPT = ChatPromptTemplate.from_messages([("system", "You are Acme's agent."), ("human", "{question}")])


def test_the_tools_are_the_kits_and_bound_to_the_customer(on_mock: Niadra) -> None:
    seed(on_mock)
    conversation = on_mock.conversation("chat-1", subject=MARINA)
    tools = history_tools(conversation)
    assert {
        t.name: {"name": t.name, "description": t.description, "parameters": t.args_schema} for t in tools
    } == (DEFINITIONS)
    search = next(t for t in tools if t.name == "search_customer_history")
    assert "4471" in search.invoke({"query": "lid"})
    with_memory = history_tools(conversation, agent_memory={"write": True})
    assert [t.name for t in with_memory][-2:] == ["search_agent_memory", "remember"]


async def test_the_tools_run_async_too(on_mock_async: AsyncNiadra) -> None:
    await seed_async(on_mock_async)
    tools = history_tools(on_mock_async.conversation("chat-1", subject=MARINA))
    search = next(t for t in tools if t.name == "search_customer_history")
    assert "4471" in await search.ainvoke({"query": "lid", "filters": {"channels": ["whatsapp"]}})


def test_a_chain_gets_the_pack_after_the_instructions(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    model = FakeChat(replies=[answer("Your new lid ships today.")])
    with on_mock.conversation("chat-1", subject=MARINA) as conversation:
        chain = PROMPT | context_runnable(conversation) | model
        handler = NiadraCallbackHandler(conversation)
        result = chain.invoke({"question": "About my lid"}, config={"callbacks": [handler]})
        assert result.content == "Your new lid ships today."
    on_mock.flush()
    (prompt,) = model.prompts
    assert isinstance(prompt[0], SystemMessage) and prompt[0].content == "You are Acme's agent."
    assert isinstance(prompt[1], SystemMessage) and EARLIER in str(prompt[1].content)
    assert isinstance(prompt[2], HumanMessage)
    assert turns(mock_app.cell, "chat-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]
    agent = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert agent.usage is not None and (agent.usage.model, agent.usage.cached_tokens) == ("gpt-4.1", 1024)
    assert agent.context_stamp is not None


def test_replayed_history_is_recorded_once(on_mock: Niadra, mock_app: MockApp) -> None:
    model = FakeChat(replies=[answer("First.", "m1"), answer("Second.", "m2")])
    with on_mock.conversation("chat-2", subject=MARINA) as conversation:
        handler = NiadraCallbackHandler(conversation)
        history: list[Any] = [SystemMessage(content="x")]
        for question in ("Question one", "Question two"):
            history.append(HumanMessage(content=question))
            history.append(model.invoke(history, config={"callbacks": [handler]}))
    on_mock.flush()
    assert turns(mock_app.cell, "chat-2") == [
        ("customer", "Question one"),
        ("ai_agent", "First."),
        ("customer", "Question two"),
        ("ai_agent", "Second."),
    ]


async def test_the_agents_notes_go_first_in_the_same_system_message(on_mock_async: AsyncNiadra) -> None:
    await seed_async(on_mock_async)
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    conversation = on_mock_async.conversation("chat-3", subject=MARINA)
    messages = await awith_context(
        conversation, [SystemMessage(content="x"), HumanMessage(content="hi")], True
    )
    slot = str(messages[1].content)
    assert slot.index("replacement order") < slot.index(EARLIER)


def test_niadra_down_leaves_the_prompt_alone(on_mock: Niadra, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    conversation = on_mock.conversation("chat-4", subject=MARINA)
    original = [SystemMessage(content="x"), HumanMessage(content="hi")]
    assert with_context(conversation, original) == original
    search = next(t for t in history_tools(conversation) if t.name == "search_customer_history")
    assert "unavailable" in json.loads(search.invoke({"query": "lid"}))["error"]


class Suspending:
    """An async session whose reads really wait, as over a network."""

    id = "chat-5"

    async def context(self) -> Any:
        await asyncio.sleep(0)
        raise AssertionError("never reached from sync code")


def test_an_async_conversation_from_sync_code_is_left_out() -> None:
    original = [HumanMessage(content="hi")]
    assert with_context(Suspending(), original) == original  # type: ignore[arg-type]


def test_with_turns_each_run_is_a_turn_record_with_its_tools_and_model(
    on_mock: Niadra, mock_app: MockApp
) -> None:
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import tool as lc_tool

    from niadra.turns import current_turn

    seen: list[Any] = []

    @lc_tool
    def stock(sku: str) -> str:
        """Units of an item in stock."""
        seen.append(current_turn())
        return f"{sku}: 3"

    model = FakeChat(replies=[answer("3 in stock.")])
    with on_mock.conversation("chat-9", subject=MARINA, agent_id="store") as conversation:
        handler = NiadraCallbackHandler(conversation, turns=True)
        chain = RunnableLambda(lambda question: [HumanMessage(stock.invoke({"sku": "PX-4471"}))]) | model
        chain.invoke("Is the PX in stock?", config={"callbacks": [handler]})
        model.invoke([HumanMessage("And the PY?")], config={"callbacks": [handler]})
    on_mock.flush()
    first, second = sorted(
        (s.record for s in mock_app.cell.turns.turns.values()), key=lambda r: r["started_at"]
    )
    assert first["build"]["adapter"].startswith("langchain/")
    (call,) = [c for c in first["calls"] if c["kind"] == "tool"]
    assert (call["name"], call["status"]) == ("stock", "ok")
    assert first["blobs"][call["result_model"]]["content"] == "PX-4471: 3"
    assert seen[0] is not None and seen[0].turn_id == first["turn_id"]
    assert [c["tokens"] for c in first["calls"] if c["kind"] == "model"] == [
        {"in": 1200, "cached": 1024, "out": 7}
    ]
    assert [c["kind"] for c in second["calls"]] == ["model"]


def test_without_turns_the_handler_records_no_turn(on_mock: Niadra, mock_app: MockApp) -> None:
    model = FakeChat(replies=[answer("Hi.")])
    with on_mock.conversation("chat-10", subject=MARINA) as conversation:
        model.invoke([HumanMessage("Hello")], config={"callbacks": [NiadraCallbackHandler(conversation)]})
    on_mock.flush()
    assert mock_app.cell.turns.turns == {}


def _replaying(tool_name: str, args: dict[str, Any], value: Any) -> Any:
    from niadra.replay.playback import Playback, _Recorded
    from niadra.turns import TurnFrame
    from niadra.turns.capture import snapshot
    from niadra.turns.digest import digest

    frame = TurnFrame(None, agent="support", conversation_id="chat-1")
    frame.playback = Playback({tool_name: [_Recorded(digest(json.loads(snapshot(args)))[0], value)]})
    return frame


def test_in_a_replay_a_replayable_tool_answers_from_the_record_and_another_is_refused(
    on_mock: Niadra,
) -> None:
    from langchain_core.tools import tool as lc_tool

    from niadra.errors import ReplayRefusedError
    from niadra.integrations.langchain import replayable

    ran: list[str] = []

    @lc_tool
    def stock(sku: str) -> str:
        """Units of an item in stock."""
        ran.append(sku)
        return f"{sku}: 0"

    (safe,) = replayable([stock])
    conversation = on_mock.conversation("chat-1", subject=MARINA)
    handler = NiadraCallbackHandler(conversation, turns=True)
    with _replaying("stock", {"sku": "PX-4471"}, "PX-4471: 3"):
        assert safe.invoke({"sku": "PX-4471"}, config={"callbacks": [handler]}) == "PX-4471: 3"
        with pytest.raises(ReplayRefusedError):
            stock.invoke({"sku": "PX-4471"}, config={"callbacks": [handler]})
    assert ran == []
    assert safe.invoke({"sku": "PX-1"}, config={"callbacks": [handler]}) == "PX-1: 0", (
        "outside a replay it runs"
    )


def test_a_wrapped_tool_is_recorded_once(on_mock: Niadra, mock_app: MockApp) -> None:
    from langchain_core.tools import tool as lc_tool

    from niadra.integrations.langchain import replayable

    @lc_tool
    def stock(sku: str) -> str:
        """Units of an item in stock."""
        return f"{sku}: 3"

    (safe,) = replayable([stock])
    with on_mock.conversation("chat-1", subject=MARINA) as conversation:
        handler = NiadraCallbackHandler(conversation, turns=True)
        with conversation.turn():
            safe.invoke({"sku": "PX-4471"}, config={"callbacks": [handler]})
    on_mock.flush()
    (record,) = [s.record for s in mock_app.cell.turns.turns.values()]
    (call,) = [c for c in record["calls"] if c["kind"] == "tool"]
    assert record["blobs"][call["args"]]["content"] == {"sku": "PX-4471"}
