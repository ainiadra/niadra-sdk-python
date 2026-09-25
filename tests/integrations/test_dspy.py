"""DSPy: real `Predict` and `ReAct` programs with the adapter, the module and the tools, DSPy's own
`DummyLM`, and the emulator behind."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("dspy")

import dspy
from dspy.utils.dummies import DummyLM

from niadra import AsyncNiadra, Niadra
from niadra.integrations.dspy import NiadraModule, history_tools, niadra_adapter
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed, turns


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    # DSPy 3.4 warns that DummyLM uses the custom-LM interface it will replace in 3.5.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.simplefilter("ignore", FutureWarning)
        yield


def lm_for(*answers: dict[str, Any]) -> DummyLM:
    return DummyLM(list(answers))


def sent(lm: DummyLM, index: int = 0) -> list[dict[str, Any]]:
    return list(lm.history[index]["messages"])


@pytest.fixture
def chat(on_mock: Niadra) -> Any:
    seed(on_mock)
    return on_mock.conversation("dspy-1", subject=MARINA)


def test_the_pack_follows_the_system_message_and_turns_are_recorded(
    chat: Any, on_mock: Niadra, mock_app: MockApp
) -> None:
    lm = lm_for({"answer": "Your new lid ships today."})
    agent = NiadraModule(dspy.Predict("question -> answer"), chat)
    with dspy.context(lm=lm):
        prediction = agent(question="About my lid")
    assert prediction.answer == "Your new lid ships today."
    messages = sent(lm)
    assert messages[0]["role"] == "system" and "question" in messages[0]["content"]
    assert messages[1]["role"] == "system" and EARLIER in messages[1]["content"]
    assert "About my lid" in messages[2]["content"]
    on_mock.flush()
    assert turns(mock_app.cell, "dspy-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]


def test_react_runs_the_kits_tools_for_the_customer(chat: Any) -> None:
    tools = history_tools(chat)
    for tool in tools:
        assert tool.format_as_litellm_function_call() == {
            "type": "function",
            "function": DEFINITIONS[tool.name],
        }
        assert tool.args == DEFINITIONS[tool.name]["parameters"]["properties"]
    lm = lm_for(
        {
            "next_thought": "Search.",
            "next_tool_name": "search_customer_history",
            "next_tool_args": {"query": "lid"},
        },
        {"next_thought": "Done.", "next_tool_name": "finish", "next_tool_args": {}},
        {"reasoning": "Found it.", "answer": "It was 4471."},
    )
    agent = NiadraModule(dspy.ReAct("question -> answer", tools=tools), chat)
    with dspy.context(lm=lm):
        prediction = agent(question="What did I report?")
    assert prediction.answer == "It was 4471."
    assert "4471" in str(prediction.trajectory["observation_0"])
    assert all(EARLIER in m[1]["content"] for m in (sent(lm, i) for i in range(3))), "every model call"


def test_the_adapter_alone_and_json_adapters(chat: Any) -> None:
    lm = DummyLM([{"answer": "ok"}], adapter=dspy.JSONAdapter())
    with dspy.context(lm=lm, adapter=niadra_adapter(chat, base=dspy.JSONAdapter)):
        dspy.Predict("question -> answer")(question="Hi")
    assert EARLIER in sent(lm)[1]["content"]


def test_the_agents_notes_and_transfers(chat: Any, on_mock: Niadra, mock_app: MockApp) -> None:
    on_mock.remember("procedure", "Replacement parts", "Open a replacement order before any refund.")
    assert [t.name for t in history_tools(chat, {"write": True})][-2:] == ["search_agent_memory", "remember"]
    lm = lm_for({"answer": "Opening it."})
    agent = NiadraModule(dspy.Predict("question -> answer"), chat, agent_memory=True)
    with dspy.context(lm=lm):
        agent(question="Hi")
    slot = sent(lm)[1]["content"]
    assert slot.index("replacement order") < slot.index(EARLIER)
    agent.transferred_to_agent("billing")
    agent.transferred_to_human("asked for a person")
    on_mock.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


def test_niadra_down_never_stops_the_program(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    lm = lm_for(
        {
            "next_thought": "Search.",
            "next_tool_name": "search_customer_history",
            "next_tool_args": {"query": "lid"},
        },
        {"next_thought": "Done.", "next_tool_name": "finish", "next_tool_args": {}},
        {"reasoning": "No history.", "answer": "Still here."},
    )
    agent = NiadraModule(dspy.ReAct("question -> answer", tools=history_tools(chat)), chat)
    with dspy.context(lm=lm):
        prediction = agent(question="Hi")
    assert prediction.answer == "Still here."
    assert not any(EARLIER in str(m["content"]) for m in sent(lm))
    assert "unavailable" in str(prediction.trajectory["observation_0"])


async def test_acall_with_the_async_client(on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    await on_mock_async.remember("pitfall", "Lids", "Lids ship apart from the body.")
    conversation = on_mock_async.conversation("dspy-async", subject=MARINA)
    lm = lm_for(
        {
            "next_thought": "Notes.",
            "next_tool_name": "search_agent_memory",
            "next_tool_args": {"query": "lids"},
        },
        {"next_thought": "Done.", "next_tool_name": "finish", "next_tool_args": {}},
        {"reasoning": "Known.", "answer": "They ship apart."},
    )
    react = dspy.ReAct("question -> answer", tools=history_tools(conversation, agent_memory=True))
    agent = NiadraModule(react, conversation, agent_memory=True)
    with dspy.context(lm=lm):
        prediction = await agent.acall(question="Where is my lid?")
    assert prediction.answer == "They ship apart."
    assert "Lids ship apart" in str(prediction.trajectory["observation_0"])
    assert "Lids ship apart" in sent(lm)[1]["content"]
    await on_mock_async.flush()
    assert turns(mock_app.cell, "dspy-async") == [
        ("customer", "Where is my lid?"),
        ("ai_agent", "They ship apart."),
    ]
