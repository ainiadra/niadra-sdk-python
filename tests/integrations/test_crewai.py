"""CrewAI: a real `Crew` with an `Agent` and a `Task`, a scripted `BaseLLM`, and the emulator behind."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

# No telemetry leaves a test run.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
pytest.importorskip("crewai")

from crewai import Agent, Crew, Task
from crewai.llms.base_llm import BaseLLM
from pydantic import Field

from niadra import Niadra
from niadra.integrations.crewai import NiadraCrew
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed, turns


class FakeLLM(BaseLLM):
    replies: list[str] = Field(default_factory=list)
    prompts: list[Any] = Field(default_factory=list)

    def call(
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        available_functions: Any = None,
        **kwargs: Any,
    ) -> str:
        self.prompts.append(messages)
        return self.replies.pop(0) if self.replies else "Thought: done\nFinal Answer: ok"

    def supports_function_calling(self) -> bool:
        return False


def crew_for(memory: NiadraCrew, llm: FakeLLM) -> Crew:
    support = Agent(
        role="Support", goal="Help the customer", backstory="You work for Acme.", tools=memory.tools, llm=llm
    )
    task = Task(
        description="Answer the customer: {message}\n\n{niadra_context}",
        expected_output="A short answer.",
        agent=support,
    )
    return Crew(
        agents=[support],
        tasks=[task],
        before_kickoff_callbacks=[memory.before_kickoff],
        after_kickoff_callbacks=[memory.after_kickoff],
    )


def prompt_text(llm: FakeLLM, index: int = 0) -> str:
    messages = llm.prompts[index]
    return messages if isinstance(messages, str) else "\n".join(str(m.get("content")) for m in messages)


def test_the_context_reaches_the_task_and_turns_are_recorded(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    conversation = on_mock.conversation("crew-1", subject=MARINA)
    llm = FakeLLM(model="fake-model", replies=["Thought: I know it\nFinal Answer: Your new lid ships today."])
    output = crew_for(NiadraCrew(conversation), llm).kickoff(inputs={"message": "About my lid"})
    assert output.raw == "Your new lid ships today."
    on_mock.flush()
    assert EARLIER in prompt_text(llm)
    assert turns(mock_app.cell, "crew-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]


def test_the_history_tools_are_the_kits_and_run_for_the_customer(on_mock: Niadra) -> None:
    seed(on_mock)
    conversation = on_mock.conversation("crew-2", subject=MARINA)
    memory = NiadraCrew(conversation)
    for tool in memory.tools:
        definition = DEFINITIONS[tool.name]
        assert tool.description == definition["description"]
        assert tool.args_schema.model_json_schema() == definition["parameters"]
    search = next(t for t in memory.tools if t.name == "search_customer_history")
    assert "4471" in search.run(query="lid")
    llm = FakeLLM(
        model="fake-model",
        replies=[
            'Thought: I should search\nAction: search_customer_history\nAction Input: {"query": "lid"}',
            "Thought: found it\nFinal Answer: It was 4471.",
        ],
    )
    output = crew_for(memory, llm).kickoff(inputs={"message": "What did I report?"})
    assert output.raw == "It was 4471."
    assert "4471" in prompt_text(llm, 1)


def test_the_agents_notes_and_transfers(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    on_mock.remember("procedure", "Replacement parts", "Open a replacement order before any refund.")
    conversation = on_mock.conversation("crew-3", subject=MARINA)
    memory = NiadraCrew(conversation, agent_memory={"write": True})
    assert [t.name for t in memory.tools][-2:] == ["search_agent_memory", "remember"]
    inputs = memory.before_kickoff({"message": "Hi"})
    assert inputs["niadra_context"].index("replacement order") < inputs["niadra_context"].index(EARLIER)
    memory.transferred_to_agent("billing")
    memory.transferred_to_human("asked for a person")
    on_mock.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


def test_niadra_down_never_stops_the_crew(on_mock: Niadra, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    conversation = on_mock.conversation("crew-4", subject=MARINA)
    llm = FakeLLM(model="fake-model", replies=["Thought: ok\nFinal Answer: Still here."])
    output = crew_for(NiadraCrew(conversation), llm).kickoff(inputs={"message": "Hi"})
    assert output.raw == "Still here."
    assert json.loads(next(t for t in NiadraCrew(conversation).tools).run(query="x"))["error"]
