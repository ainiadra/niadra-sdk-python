"""Pipecat: the customer's memory in a Pipecat pipeline, as one frame processor.

```python
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from niadra import AsyncNiadra, phone
from niadra.integrations.pipecat import NiadraMemoryProcessor, history_tools

niadra = AsyncNiadra(channel="voice")
conversation = niadra.conversation(call_sid, subject=phone(caller), channel="voice", view="voice")
memory = NiadraMemoryProcessor(conversation, attestation=stir_verstat)

context = LLMContext(messages, tools=history_tools(conversation))
aggregators = LLMContextAggregatorPair(context)
memory.observe(aggregators)
user, assistant = aggregators.user(), aggregators.assistant()
pipeline = Pipeline(
    [transport.input(), stt, memory.prefetcher(), user, memory, llm, tts, transport.output(), assistant]
)
```

- **Context.** On each `LLMContextFrame`, between the user aggregator and the LLM, the processor
  reads `context()` (150 ms for the voice view) and puts the pack right after the leading
  `system` or `developer` messages and the turn block at the end. The blocks it placed on the
  previous turn are taken out first, so the shared context never piles them up. A speculative
  inference gets them too, in its provisional copy. The read sends the user's turn (the last
  user message of the context) along, so a space with memory v2 answers with what that turn
  needs from memory, last in the turn block.
- **Prefetch.** `prefetcher()` is a second processor, for right after the STT service (the user
  aggregator consumes the interim transcripts): on each `InterimTranscriptionFrame` and
  `TranscriptionFrame` it sends the turn so far with `prefetch()`, in the background, so the
  read that answers the turn finds the caller's memory warm. It passes every frame on at once
  and never holds or fails a turn. Leave it out and nothing else changes.
- **Turns.** `observe()` subscribes to the aggregators: each user message written to the
  context (`on_user_turn_message_added`, final in cascade and realtime modes) is the customer's
  turn and each finished assistant turn (`on_assistant_turn_stopped`) is the agent's. `EndFrame`
  and `CancelFrame` end the conversation.
- **Tools.** `history_tools()` gives the three history tools as `FunctionSchema`s that carry their
  handlers, so the LLM service registers them from the context. Their JSON Schemas are the kit's.
- **Agent memory.** With `agent_memory=True` (or `{"write": True, "max_tokens": 300, "tags": [...]}`)
  the agent's own notes go right before the customer's context, in the same message, and
  `history_tools(conversation, agent_memory=...)` adds `search_agent_memory` (and `remember`).
- **Verification.** `attestation=` is the carrier's STIR/SHAKEN level (`A`, `B` or `C`, or
  Twilio's `StirVerstat`), verified once before the first context.
- **Handoff.** `transferred_to_human()` and `transferred_to_agent()` record a transfer; call them
  where your pipeline (or Pipecat Flows) hands the call over.

The shape of the processor (between the user aggregator and the LLM, editing the context of
the frame and always passing it on, even on failure) follows Pipecat's own Mem0 memory
service, `pipecat/services/mem0/memory.py`, Copyright (c) 2024-2026, Daily, BSD 2-Clause
License. The reading is Niadra's: one pinned pack per conversation instead of a similarity
search per message.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.frames.frames import (
        CancelFrame,
        EndFrame,
        Frame,
        InterimTranscriptionFrame,
        LLMContextFrame,
        TranscriptionFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("Pipecat is not installed: pip install 'niadra[pipecat]'") from exc

from niadra._async_client import AsyncNiadra
from niadra.conversation import AsyncConversation
from niadra.integrations._common import (
    AgentMemoryLike,
    AnyKit,
    agent_turn,
    call_tool,
    customer_turn,
    end,
    handoff,
    instruction_count,
    mark_injected,
    memory_kit_of,
    memory_option,
    phone_or_none,
    prefetch,
    read_prompt,
    tool_specs,
    verify_attestation,
    warn,
)
from niadra.models.common import Handle

__all__ = ["NiadraMemoryProcessor", "NiadraPrefetchProcessor", "conversation_for_call", "history_tools"]


def conversation_for_call(
    niadra: AsyncNiadra,
    call_id: str,
    caller: str | Handle | None,
    *,
    channel: str = "voice",
    view: str = "voice",
    agent_id: str | None = None,
) -> AsyncConversation:
    """The conversation of a phone call: the call id (Twilio's `CallSid`, Daily's `callId`) and the
    caller's number in E.164 (`From`), or a handle you resolved yourself."""
    subject = caller if isinstance(caller, Handle) else phone_or_none(caller)
    return niadra.conversation(call_id, subject=subject, channel=channel, view=view, agent_id=agent_id)


class _HistorySchema(FunctionSchema):
    """A `FunctionSchema` whose parameters are the kit's JSON Schema as it is, not rebuilt."""

    def __init__(self, name: str, description: str, parameters: dict[str, Any], handler: Any) -> None:
        super().__init__(
            name=name,
            description=description,
            properties=parameters.get("properties", {}),
            required=list(parameters.get("required", [])),
            handler=handler,
        )
        self._parameters = parameters

    def to_default_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self._parameters}


def history_tools(conversation: AsyncConversation, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as `FunctionSchema`s with their handlers, bound to the customer, with the
    agent memory tools when `agent_memory` asks for them."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    specs = tool_specs(kit.definitions)
    return [_HistorySchema(s.name, s.description, s.parameters, _handler(kit, s.name)) for s in specs]


def _handler(kit: AnyKit, name: str) -> Callable[[Any], Any]:
    async def handle(params: Any) -> None:
        await params.result_callback(await call_tool(kit, name, dict(params.arguments or {})))

    return handle


class NiadraMemoryProcessor(FrameProcessor):
    """Puts the customer's context into every inference and records the conversation.

    Place it between the user context aggregator and the LLM service, and call `observe()` with
    the aggregator pair so it can record turns.
    """

    def __init__(
        self,
        conversation: AsyncConversation,
        *,
        attestation: str | None = None,
        role: str = "system",
        agent_memory: AgentMemoryLike = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.conversation = conversation
        self.attestation = attestation
        self.agent_memory = memory_option(agent_memory)
        self.role = role
        self._verified = False
        self._placed: list[dict[str, Any]] = []
        self._heard: list[str] = []

    def prefetcher(self, **kwargs: Any) -> NiadraPrefetchProcessor:
        """The processor that prefetches the caller's turn: put it right after the STT service."""
        return NiadraPrefetchProcessor(self, **kwargs)

    def _hearing(self, frame: Frame) -> None:
        """Sends the turn so far: the final segments of the turn and, when interim, `frame`'s text."""
        text = str(getattr(frame, "text", "") or "").strip()
        if isinstance(frame, TranscriptionFrame):
            if text:
                self._heard.append(text)
            heard = " ".join(self._heard)
        else:
            heard = " ".join([*self._heard, text] if text else self._heard)
        prefetch(self.conversation, heard)

    def observe(self, aggregators: Any) -> None:
        """Records turns from an `LLMContextAggregatorPair` (or a `(user, assistant)` pair)."""
        user, assistant = (
            (aggregators.user(), aggregators.assistant())
            if hasattr(aggregators, "user")
            else tuple(aggregators)
        )
        user.add_event_handler("on_user_turn_message_added", self._on_user_message)
        assistant.add_event_handler("on_assistant_turn_stopped", self._on_assistant_turn)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the call went to a person."""
        handoff(self.conversation, "human", reason=reason)

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the call over; it reads the same conversation."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            try:
                await self._place(frame.context, speculative=frame.speculation)
            except Exception as exc:
                warn("place the context", exc)
        elif isinstance(frame, (EndFrame, CancelFrame)):
            end(self.conversation)
        await self.push_frame(frame, direction)

    async def _place(self, context: Any, *, speculative: bool) -> None:
        messages = [m for m in context.get_messages() if not any(m is placed for placed in self._placed)]
        if not self._verified:
            self._verified = True
            await verify_attestation(self.conversation, self.attestation)
        if not speculative:
            self._heard = []
        prompt = await read_prompt(self.conversation, self.agent_memory, turn=_last_user_text(messages))
        placed: list[dict[str, Any]] = []
        if prompt.system:
            block = {"role": self.role, "content": prompt.system}
            messages.insert(instruction_count(messages), block)
            placed.append(block)
        if prompt.turn:
            block = {"role": self.role, "content": prompt.turn}
            messages.append(block)
            placed.append(block)
        context.set_messages(messages)
        if prompt.context is not None:
            mark_injected(self.conversation, prompt.context)
        # A speculative inference runs on a provisional copy: the shared context keeps what it had.
        if not speculative:
            self._placed = placed
        else:
            self._placed = [*self._placed, *placed]

    async def _on_user_message(self, _aggregator: Any, message: Any) -> None:
        customer_turn(self.conversation, getattr(message, "content", None))

    async def _on_assistant_turn(self, _aggregator: Any, message: Any) -> None:
        agent_turn(self.conversation, getattr(message, "content", None))


class NiadraPrefetchProcessor(FrameProcessor):
    """Prefetches the caller's turn from the STT service's transcripts; see
    `NiadraMemoryProcessor.prefetcher()`. Every frame passes through unchanged."""

    def __init__(self, memory: NiadraMemoryProcessor, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.memory = memory

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)):
            try:
                self.memory._hearing(frame)
            except Exception as exc:
                warn("prefetch the turn", exc)


def _last_user_text(messages: list[Any]) -> str | None:
    """The caller's turn this inference answers: the text of the last user message."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                p.get("text") for p in content if isinstance(p, dict) and p.get("type", "text") == "text"
            ]
            return "".join(p for p in parts if isinstance(p, str)) or None
        return None
    return None
