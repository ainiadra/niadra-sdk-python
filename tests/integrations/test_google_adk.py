"""Google ADK: a real `LlmAgent` run by the `InMemoryRunner`, a scripted `BaseLlm`, and the
emulator behind."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

pytest.importorskip("google.adk")

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import Field

from niadra import AsyncNiadra
from niadra.integrations.google_adk import NiadraADK
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed_async, turns

USAGE = types.GenerateContentResponseUsageMetadata(
    prompt_token_count=1400, cached_content_token_count=1024, candidates_token_count=9, total_token_count=1409
)


def text(value: str) -> LlmResponse:
    content = types.Content(role="model", parts=[types.Part(text=value)])
    return LlmResponse(content=content, usage_metadata=USAGE, model_version="gemini-2.5-flash")


def call(name: str, args: dict[str, Any]) -> LlmResponse:
    content = types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]
    )
    return LlmResponse(content=content)


class Seen:
    def __init__(self, request: LlmRequest) -> None:
        self.system = str(request.config.system_instruction or "")
        self.contents = list(request.contents)


class FakeGemini(BaseLlm):
    replies: list[LlmResponse]
    requests: list[Any] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(Seen(llm_request))
        yield self.replies.pop(0) if self.replies else text("ok")


async def run(agent: LlmAgent, *messages: str) -> list[Any]:
    runner = InMemoryRunner(agent=agent, app_name="acme")
    session = await runner.session_service.create_session(app_name="acme", user_id="u1")
    events: list[Any] = []
    for message in messages:
        content = types.Content(role="user", parts=[types.Part(text=message)])
        async for event in runner.run_async(user_id="u1", session_id=session.id, new_message=content):
            events.append(event)
    return events


def agent_for(memory: NiadraADK, model: FakeGemini, **options: Any) -> LlmAgent:
    return LlmAgent(
        name="support",
        model=model,
        instruction="You are Acme's agent.",
        tools=memory.tools,
        before_model_callback=memory.before_model,
        after_model_callback=memory.after_model,
        **options,
    )


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("adk-1", subject=MARINA)


async def test_the_pack_follows_the_instruction_and_turns_are_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeGemini(model="fake", replies=[text("Your new lid ships today."), text("You're welcome.")])
    await run(agent_for(NiadraADK(chat), model), "About my lid", "Thanks")
    await on_mock_async.flush()
    system = model.requests[0].system
    assert system.index("You are Acme's agent.") < system.index(EARLIER)
    assert turns(mock_app.cell, "adk-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
        ("customer", "Thanks"),
        ("ai_agent", "You're welcome."),
    ]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None and (answer.usage.model, answer.usage.cached_tokens) == (
        "gemini-2.5-flash",
        1024,
    )


async def test_the_history_tools_are_the_kits_and_run_for_the_customer(
    chat: Any, on_mock_async: AsyncNiadra
) -> None:
    memory = NiadraADK(chat)
    declared = {t.name: t._get_declaration() for t in memory.tools}
    for name, declaration in declared.items():
        assert declaration.description == DEFINITIONS[name]["description"]
        assert declaration.parameters_json_schema == DEFINITIONS[name]["parameters"]
    model = FakeGemini(
        model="fake", replies=[call("search_customer_history", {"query": "lid"}), text("It was 4471.")]
    )
    events = await run(agent_for(memory, model), "What did I report?")
    responses = [
        p.function_response
        for e in events
        for p in (e.content.parts if e.content else [])
        if p.function_response
    ]
    assert "4471" in str(responses[0].response)
    await on_mock_async.flush()


async def test_a_transfer_between_agents_is_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    memory = NiadraADK(chat)
    billing = LlmAgent(
        name="billing",
        model=FakeGemini(model="fake", replies=[text("Billing here.")]),
        instruction="Billing.",
    )
    model = FakeGemini(model="fake", replies=[call("transfer_to_agent", {"agent_name": "billing"})])
    await run(agent_for(memory, model, sub_agents=[billing]), "My invoice")
    memory.transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [(h.target, h.reason) for h in items(mock_app.cell, "handoff")] == [
        ("agent", "transfer to billing"),
        ("human", "asked for a person"),
    ]


async def test_the_agents_notes_come_first(chat: Any, on_mock_async: AsyncNiadra) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    memory = NiadraADK(chat, agent_memory={"write": True})
    assert [t.name for t in memory.tools][-2:] == ["search_agent_memory", "remember"]
    model = FakeGemini(model="fake", replies=[text("Opening it.")])
    await run(agent_for(memory, model), "Hi")
    system = model.requests[0].system
    assert system.index("replacement order") < system.index(EARLIER)


async def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    model = FakeGemini(model="fake", replies=[text("Still here.")])
    events = await run(agent_for(NiadraADK(chat), model), "Hi")
    assert any(e.content and e.content.parts and e.content.parts[0].text == "Still here." for e in events)
    assert model.requests[0].system.startswith("You are Acme's agent.")
    assert EARLIER not in model.requests[0].system


def stock(sku: str) -> dict[str, Any]:
    """Units of an item in stock."""
    return {"sku": sku, "qty": 3}


def turned(memory: NiadraADK, model: FakeGemini, **options: Any) -> LlmAgent:
    return agent_for(
        memory,
        model,
        before_agent_callback=memory.before_agent,
        after_agent_callback=memory.after_agent,
        before_tool_callback=memory.before_tool,
        after_tool_callback=memory.after_tool,
        on_tool_error_callback=memory.on_tool_error,
        **options,
    )


async def test_with_turns_each_run_is_a_turn_with_its_tools_and_model_calls(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    memory = NiadraADK(chat, turns=True)
    memory.tools.append(stock)
    model = FakeGemini(model="fake", replies=[call("stock", {"sku": "PX-4471"}), text("3 in stock.")])
    await run(turned(memory, model), "Is the PX in stock?")
    await on_mock_async.flush()
    (record,) = [stored.record for stored in mock_app.cell.turns.turns.values()]
    assert record["agent"] == {"name": "support"}
    assert record["build"]["adapter"].startswith("google_adk/")
    (tool,) = [c for c in record["calls"] if c["kind"] == "tool"]
    assert tool["name"] == "stock" and tool["status"] == "ok"
    assert record["blobs"][tool["args"]]["content"] == {"sku": "PX-4471"}
    assert record["blobs"][tool["result_model"]]["content"] == {"sku": "PX-4471", "qty": 3}
    models = [c for c in record["calls"] if c["kind"] == "model"]
    assert [m.get("name") for m in models] == [None, "gemini-2.5-flash"]  # the first reported no model
    assert models[1]["tokens"] == {"in": 1400, "cached": 1024, "out": 9}
    assert len(record["output"]["event_keys"]) == 1


async def test_without_turns_nothing_is_recorded(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    memory = NiadraADK(chat)
    await run(turned(memory, FakeGemini(model="fake", replies=[text("Hi.")])), "Hello")
    await on_mock_async.flush()
    assert mock_app.cell.turns.turns == {}


async def test_an_agent_used_as_a_tool_is_a_sub_turn(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    from google.adk.tools.agent_tool import AgentTool

    memory = NiadraADK(chat, turns=True)
    pricing = LlmAgent(
        name="pricing",
        model=FakeGemini(model="fake", replies=[text("R$ 199,90")]),
        instruction="Prices.",
        before_agent_callback=memory.before_agent,
        after_agent_callback=memory.after_agent,
    )
    model = FakeGemini(model="fake", replies=[call("pricing", {"request": "PX"}), text("It is R$ 199,90.")])
    agent = LlmAgent(
        name="support",
        model=model,
        instruction="You are Acme's agent.",
        tools=[AgentTool(agent=pricing)],
        before_agent_callback=memory.before_agent,
        after_agent_callback=memory.after_agent,
        before_tool_callback=memory.before_tool,
        after_tool_callback=memory.after_tool,
    )
    await run(agent, "How much is the PX?")
    await on_mock_async.flush()
    records = {r["agent"]["name"]: r for r in (s.record for s in mock_app.cell.turns.turns.values())}
    assert set(records) == {"support", "pricing"}
    assert records["pricing"]["agent"]["parent_turn_id"] == records["support"]["turn_id"]


async def test_in_a_replay_a_tool_answers_from_the_record_and_never_runs(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    import json

    from niadra.replay.playback import Playback, _Recorded
    from niadra.turns import TurnFrame
    from niadra.turns.capture import snapshot
    from niadra.turns.digest import digest

    ran: list[str] = []

    def reserve(sku: str) -> dict[str, Any]:
        """Holds one unit of an item."""
        ran.append(sku)
        return {"sku": sku, "held": True}

    memory = NiadraADK(chat, turns=True)
    memory.tools.append(reserve)
    recorded = _Recorded(
        digest(json.loads(snapshot({"sku": "PX-4471"})))[0], {"sku": "PX-4471", "held": False}
    )
    frame = TurnFrame(None, agent="support", conversation_id="adk-1")
    frame.playback = Playback({"reserve": [recorded]})
    model = FakeGemini(
        model="fake", replies=[call("reserve", {"sku": "PX-4471"}), text("Could not hold it.")]
    )
    with frame:
        await run(turned(memory, model), "Hold one PX for me.")
    assert ran == []
    await on_mock_async.flush()
    assert mock_app.cell.turns.turns == {}
