"""LiveKit Agents: a real `AgentSession` and `Agent`, text turns, a fake LLM, the emulator behind."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents import Agent, AgentSession, APIConnectOptions, llm
from livekit.agents.llm import ChatChunk, ChoiceDelta, CompletionUsage, FunctionToolCall

from niadra import AsyncNiadra
from niadra.integrations.livekit import NiadraAgent, NiadraMemory, conversation_for, history_tools
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed_async, turns

Reply = str | tuple[str, dict[str, Any]]


class FakeLLM(llm.LLM):
    """Answers each call with the next scripted reply: text, or a tool call `(name, arguments)`."""

    def __init__(self, *replies: Reply) -> None:
        super().__init__()
        self.replies = list(replies)
        self.prompts: list[llm.ChatContext] = []
        self.tools: list[list[Any]] = []

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def provider(self) -> str:
        return "fake"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[Any] | None = None,
        conn_options: APIConnectOptions = APIConnectOptions(),  # noqa: B008
        **_: Any,
    ) -> llm.LLMStream:
        self.prompts.append(chat_ctx.copy())
        self.tools.append(list(tools or []))
        reply = self.replies.pop(0) if self.replies else "ok"
        return FakeStream(self, reply, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class FakeStream(llm.LLMStream):
    def __init__(self, model: FakeLLM, reply: Reply, **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.reply = reply

    async def _run(self) -> None:
        if isinstance(self.reply, tuple):
            name, arguments = self.reply
            call = FunctionToolCall(name=name, arguments=json.dumps(arguments), call_id="call_1")
            self._event_ch.send_nowait(
                ChatChunk(id="c", delta=ChoiceDelta(role="assistant", tool_calls=[call]))
            )
            return
        self._event_ch.send_nowait(ChatChunk(id="c", delta=ChoiceDelta(role="assistant", content=self.reply)))
        usage = CompletionUsage(
            completion_tokens=6, prompt_tokens=1200, prompt_cached_tokens=1024, total_tokens=1206
        )
        self._event_ch.send_nowait(ChatChunk(id="c", usage=usage))


def messages(chat_ctx: llm.ChatContext) -> list[tuple[str, str]]:
    return [(i.role, i.text_content or "") for i in chat_ctx.items if i.type == "message"]


@pytest.fixture
async def call(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("call-1", subject=MARINA, channel="voice", view="voice")


async def run(agent: Agent, model: FakeLLM, *inputs: str) -> None:
    async with AgentSession(llm=model) as session:
        await session.start(agent)
        for text in inputs:
            await session.run(user_input=text)


async def test_the_pack_goes_after_the_instructions_and_news_at_the_end(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeLLM("Your new lid ships today.", "You're welcome.")
    async with AgentSession(llm=model) as session:
        await session.start(NiadraAgent(call, instructions="You are Acme's agent."))
        await session.run(user_input="Hi, about my lid")
        on_mock_async.track(
            {
                "channel": "whatsapp",
                "conversation_id": "wa-2",
                "handles": [MARINA],
                "speaker": {"role": "customer"},
                "content": {"text": "I just sent a photo of the lid"},
            }
        )
        await on_mock_async.flush()
        await session.run(user_input="Thanks")

    for prompt in model.prompts:
        shown = messages(prompt)
        assert shown[0] == ("system", "You are Acme's agent.")
        assert shown[1][0] == "system" and EARLIER in shown[1][1], "the pack right after the instructions"
        assert sum(EARLIER in text for _, text in shown) == 1, "one pack per call, nothing piles up"
    first, second = messages(model.prompts[0]), messages(model.prompts[1])
    assert first[2:] == [("user", "Hi, about my lid")]
    assert second[2:-1] == [
        ("user", "Hi, about my lid"),
        ("assistant", "Your new lid ships today."),
        ("user", "Thanks"),
    ]
    assert second[-1][0] == "system" and "sent a photo" in second[-1][1], "news from WhatsApp at the end"
    assert call.context_injected_at is not None


async def test_each_turn_is_recorded_once_and_the_call_ends(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeLLM("Your new lid ships today.")
    await run(NiadraAgent(call, instructions="You are Acme's agent."), model, "Hi, about my lid")
    await on_mock_async.flush()

    assert turns(mock_app.cell, "call-1") == [
        ("customer", "Hi, about my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None
    assert (answer.usage.provider, answer.usage.model) == ("fake", "fake-model")
    assert (answer.usage.prompt_tokens, answer.usage.cached_tokens) == (1200, 1024)
    assert answer.context_stamp is not None
    assert "call-1" in mock_app.cell.ended


async def test_history_tools_are_the_kits_and_bound_to_the_caller(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeLLM(("search_customer_history", {"query": "lid"}), "Found it: the lid of order 4471.")
    await run(NiadraAgent(call, instructions="You are Acme's agent."), model, "What did I report?")
    await on_mock_async.flush()

    offered = {tool.info.name: tool.info.raw_schema for tool in model.tools[0]}
    assert offered == {name: {**d} for name, d in DEFINITIONS.items()}
    for schema in offered.values():
        assert "subject" not in json.dumps(schema["parameters"])
    output = next(i for i in model.prompts[1].items if i.type == "function_call_output")
    assert "4471" in output.output, "the search ran for the caller bound in the kit"
    assert turns(mock_app.cell, "call-1")[-1] == ("ai_agent", "Found it: the lid of order 4471.")


async def test_the_carriers_attestation_is_verified_before_the_first_context(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeLLM("Hello Marina.")
    agent = NiadraAgent(call, attestation="A", instructions="You are Acme's agent.")
    await run(agent, model, "Hi")
    verified = items(mock_app.cell, "verify")
    assert [(v.method, v.level.value) for v in verified] == [("network_attestation", "V2")]
    assert mock_app.cell.level("call-1").value == "V2"
    assert call.verification.value == "V2"


async def test_a_handoff_between_agents_is_recorded(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    class Billing(NiadraMemory, Agent):  # type: ignore[misc]
        pass

    billing = Billing(conversation=call, instructions="You handle billing.")

    class Reception(NiadraMemory, Agent):  # type: ignore[misc]
        @llm.function_tool
        async def to_billing(self) -> Agent:
            """Transfer the caller to billing."""
            return billing

    model = FakeLLM(("to_billing", {}), "Billing here, how can I help?")
    await run(Reception(conversation=call, instructions="You are the receptionist."), model, "My invoice")
    await on_mock_async.flush()

    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent"]
    assert set(DEFINITIONS) <= {tool.info.name for tool in billing.tools}, (
        "the next agent reads the history too"
    )


async def test_a_transfer_to_a_person_is_recorded(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    NiadraAgent(call, instructions="x").transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [(h.target, h.reason) for h in items(mock_app.cell, "handoff")] == [
        ("human", "asked for a person")
    ]


async def test_niadra_down_never_stops_the_agent(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    model = FakeLLM(("search_customer_history", {"query": "lid"}), "Sorry, let me help anyway.")
    await run(NiadraAgent(call, attestation="B", instructions="You are Acme's agent."), model, "Hi")
    assert messages(model.prompts[0]) == [("system", "You are Acme's agent."), ("user", "Hi")]
    output = next(i for i in model.prompts[1].items if i.type == "function_call_output")
    assert "unavailable" in output.output


async def test_without_a_key_the_agent_runs_as_if_niadra_were_not_there() -> None:
    niadra = AsyncNiadra()
    conversation = niadra.conversation("call-2", subject=MARINA, channel="voice", view="voice")
    model = FakeLLM("Hello.")
    await run(NiadraAgent(conversation, instructions="Hi"), model, "Hi")
    assert messages(model.prompts[0]) == [("system", "Hi"), ("user", "Hi")]
    await niadra.close()


async def test_a_realtime_turn_gets_the_context_in_the_turn_hook(
    call: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = NiadraAgent(call, instructions="x")
    monkeypatch.setattr(NiadraAgent, "_niadra_realtime", lambda self: True)
    turn = llm.ChatContext()
    turn.add_message(role="system", content="You are Acme's agent.")
    turn.add_message(role="user", content="earlier question")
    await agent.on_user_turn_completed(turn, llm.ChatMessage(role="user", content=["Hi"]))
    shown = messages(turn)
    assert shown[0] == ("system", "You are Acme's agent.")
    assert EARLIER in shown[1][1] and shown[2] == ("user", "earlier question")


def test_conversation_for_reads_the_sip_participant(on_mock_async: AsyncNiadra) -> None:
    caller = SimpleNamespace(
        identity="sip_+5511912345678",
        attributes={"sip.phoneNumber": "+5511912345678", "sip.callID": "SCL_abc"},
    )
    conversation = conversation_for(on_mock_async, caller, room=SimpleNamespace(name="room-9"))
    assert (conversation.id, conversation.subject, conversation.view) == ("SCL_abc", MARINA, "voice")

    web = SimpleNamespace(identity="user-42", attributes={})
    conversation = conversation_for(on_mock_async, web, room=SimpleNamespace(name="room-9"))
    assert conversation.id == "room-9"
    assert conversation.subject is not None and conversation.subject.value == "user-42"
    assert conversation_for(on_mock_async, web, subject=MARINA).subject == MARINA


def test_no_tools_without_a_customer(on_mock_async: AsyncNiadra) -> None:
    assert history_tools(on_mock_async.conversation("c", channel="voice")) == []


async def test_the_agents_own_notes_come_before_the_customers_context(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    model = FakeLLM("I'll open the replacement order.")
    agent = NiadraAgent(call, instructions="You are Acme's agent.", agent_memory={"write": True})
    await run(agent, model, "My lid is broken")
    slot = messages(model.prompts[0])[1][1]
    assert slot.startswith("<agent_notes>") and "Open a replacement order" in slot
    assert slot.index("</agent_notes>") < slot.index("<context"), "notes first, then the customer"
    offered = [tool.info.name for tool in model.tools[0]]
    assert offered == [*DEFINITIONS, "search_agent_memory", "remember"]
    reader = NiadraAgent(call, instructions="x", agent_memory=True)
    assert [tool.info.name for tool in reader.tools][-1] == "search_agent_memory"
