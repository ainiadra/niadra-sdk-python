"""Pipecat: a real pipeline with the context aggregators, user turns driven by transcription
frames, a fake LLM service, and the emulator behind."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import (
    Frame,
    FunctionCallFromLLM,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.turns.user_start import ExternalUserTurnStartStrategy
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.time import time_now_iso8601

from niadra import AsyncNiadra
from niadra.integrations.pipecat import NiadraMemoryProcessor, conversation_for_call, history_tools
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed_async, turns

INSTRUCTIONS = {"role": "system", "content": "You are Acme's agent."}
Reply = str | tuple[str, dict[str, Any]]


class FakeLLM(LLMService):
    """Answers each context with the next scripted reply: text, or a tool call `(name, arguments)`."""

    def __init__(self, *replies: Reply) -> None:
        none: dict[str, Any] = dict.fromkeys(
            (
                "system_instruction",
                "temperature",
                "max_tokens",
                "top_p",
                "top_k",
                "frequency_penalty",
                "presence_penalty",
                "seed",
                "filter_incomplete_user_turns",
                "user_turn_completion_config",
            )
        )
        super().__init__(settings=LLMSettings(model="fake-model", **none))
        self.replies = list(replies)
        self.prompts: list[list[dict[str, Any]]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        self.prompts.append([dict(m) for m in frame.context.get_messages()])
        reply = self.replies.pop(0) if self.replies else "ok"
        await self.push_frame(LLMFullResponseStartFrame())
        if isinstance(reply, tuple):
            call = FunctionCallFromLLM(
                function_name=reply[0], tool_call_id="t1", arguments=reply[1], context=frame.context
            )
            await self.run_function_calls([call])
        else:
            await self.push_frame(LLMTextFrame(reply))
        await self.push_frame(LLMFullResponseEndFrame())


def said(*texts: str) -> list[Frame]:
    frames: list[Frame] = []
    for text in texts:
        frames += [
            UserStartedSpeakingFrame(),
            TranscriptionFrame(text, "caller", time_now_iso8601()),
            UserStoppedSpeakingFrame(),
            SleepFrame(0.3),
        ]
    return frames


def pipeline(memory: NiadraMemoryProcessor, model: FakeLLM, context: LLMContext) -> Pipeline:
    strategies = UserTurnStrategies(
        start=[ExternalUserTurnStartStrategy()], stop=[ExternalUserTurnStopStrategy(timeout=0.01)]
    )
    pair = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(user_turn_strategies=strategies)
    )
    memory.observe(pair)
    return Pipeline([pair.user(), memory, model, pair.assistant()])


@pytest.fixture
async def call(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("CA-1", subject=MARINA, channel="voice", view="voice")


async def test_the_pack_goes_after_the_instructions_and_never_piles_up(
    call: Any, on_mock_async: AsyncNiadra
) -> None:
    model = FakeLLM("Your new lid ships today.", "You're welcome.")
    context = LLMContext([INSTRUCTIONS])
    await run_test(pipeline(NiadraMemoryProcessor(call), model, context), frames_to_send=said("Hi", "Thanks"))

    for prompt in model.prompts:
        assert prompt[0] == INSTRUCTIONS
        assert prompt[1]["role"] == "system" and EARLIER in prompt[1]["content"]
        assert sum(EARLIER in str(m["content"]) for m in prompt) == 1
    assert [m["content"] for m in model.prompts[1][2:]] == ["Hi", "Your new lid ships today.", "Thanks"]
    kept = context.get_messages()
    assert sum(EARLIER in str(m["content"]) for m in kept) <= 1, "the shared context holds one pack at most"
    assert call.context_injected_at is not None


async def test_news_from_another_channel_goes_at_the_end(call: Any, on_mock_async: AsyncNiadra) -> None:
    on_mock_async.track(
        {
            "channel": "whatsapp",
            "conversation_id": "wa-2",
            "handles": [MARINA],
            "speaker": {"role": "customer"},
            "content": {"text": "I just sent a photo of the lid"},
        }
    )
    await call.context()  # the pack is pinned before the news
    await on_mock_async.flush()
    model = FakeLLM("Got the photo.")
    await run_test(
        pipeline(NiadraMemoryProcessor(call), model, LLMContext([INSTRUCTIONS])), frames_to_send=said("Hi")
    )
    last = model.prompts[0][-1]
    assert last["role"] == "system" and "sent a photo" in last["content"]


async def test_each_turn_is_recorded_once_and_the_call_ends(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    model = FakeLLM("Your new lid ships today.")
    await run_test(
        pipeline(NiadraMemoryProcessor(call), model, LLMContext([INSTRUCTIONS])), frames_to_send=said("Hi")
    )
    await on_mock_async.flush()
    assert turns(mock_app.cell, "CA-1") == [("customer", "Hi"), ("ai_agent", "Your new lid ships today.")]
    assert "CA-1" in mock_app.cell.ended


async def test_history_tools_are_the_kits_and_bound_to_the_caller(
    call: Any, on_mock_async: AsyncNiadra
) -> None:
    tools = history_tools(call)
    assert {t.name: t.to_default_dict() for t in tools} == DEFINITIONS
    model = FakeLLM(("search_customer_history", {"query": "lid"}), "Found it.")
    context = LLMContext([INSTRUCTIONS], tools=tools)
    await run_test(
        pipeline(NiadraMemoryProcessor(call), model, context), frames_to_send=said("What did I say?")
    )
    result = next(m for m in model.prompts[1] if m["role"] == "tool")
    assert "4471" in result["content"]


async def test_the_carriers_attestation_is_verified_before_the_first_context(
    call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    memory = NiadraMemoryProcessor(call, attestation="TN-Validation-Passed-B")
    await run_test(pipeline(memory, FakeLLM("Hello."), LLMContext([INSTRUCTIONS])), frames_to_send=said("Hi"))
    assert [(v.method, v.level.value) for v in items(mock_app.cell, "verify")] == [
        ("network_attestation", "V1")
    ]
    assert call.verification.value == "V1"


async def test_transfers_are_recorded(call: Any, on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    memory = NiadraMemoryProcessor(call)
    memory.transferred_to_agent("billing", target_source="billing-agent")
    memory.transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [(h.target, h.reason) for h in items(mock_app.cell, "handoff")] == [
        ("agent", "billing"),
        ("human", "asked for a person"),
    ]


async def test_a_speculative_inference_leaves_the_shared_context_alone(call: Any) -> None:
    memory = NiadraMemoryProcessor(call)
    shared = LLMContext([INSTRUCTIONS, {"role": "user", "content": "Hi"}])
    await memory._place(shared, speculative=False)
    provisional = LLMContext([*shared.get_messages(), {"role": "user", "content": "and the lid"}])
    await memory._place(provisional, speculative=True)
    assert sum(EARLIER in str(m["content"]) for m in provisional.get_messages()) == 1
    assert sum(EARLIER in str(m["content"]) for m in shared.get_messages()) == 1
    await memory._place(shared, speculative=False)
    assert sum(EARLIER in str(m["content"]) for m in shared.get_messages()) == 1


async def test_niadra_down_never_stops_the_pipeline(call: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    model = FakeLLM(("search_customer_history", {"query": "lid"}), "Let me help anyway.")
    context = LLMContext([INSTRUCTIONS], tools=history_tools(call))
    memory = NiadraMemoryProcessor(call, attestation="A")
    await run_test(pipeline(memory, model, context), frames_to_send=said("Hi"))
    assert model.prompts[0] == [INSTRUCTIONS, {"role": "user", "content": "Hi"}]
    result = next(m for m in model.prompts[1] if m["role"] == "tool")
    assert "unavailable" in result["content"]


def test_conversation_for_call(on_mock_async: AsyncNiadra) -> None:
    conversation = conversation_for_call(on_mock_async, "CA-9", "+5511912345678")
    assert (conversation.id, conversation.subject, conversation.view) == ("CA-9", MARINA, "voice")
    assert conversation_for_call(on_mock_async, "CA-9", "anonymous").subject is None
    assert history_tools(conversation_for_call(on_mock_async, "CA-9", None)) == []


async def test_the_agents_own_notes_come_before_the_customers_context(
    call: Any, on_mock_async: AsyncNiadra
) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    model = FakeLLM("Opening it.")
    memory = NiadraMemoryProcessor(call, agent_memory=True)
    tools = history_tools(call, agent_memory={"write": True})
    assert [t.name for t in tools][-2:] == ["search_agent_memory", "remember"]
    await run_test(
        pipeline(memory, model, LLMContext([INSTRUCTIONS], tools=tools)), frames_to_send=said("Hi")
    )
    slot = model.prompts[0][1]["content"]
    assert slot.startswith('<agent_memory source="niadra">') and EARLIER in slot
    assert slot.index("</agent_memory>") < slot.index("<context")
