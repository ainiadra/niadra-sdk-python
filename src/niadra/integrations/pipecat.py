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
pipeline = Pipeline([transport.input(), stt, user, memory, llm, tts, transport.output(), assistant])
```

- **Context.** On each `LLMContextFrame`, between the user aggregator and the LLM, the processor
  reads `context()` (150 ms for the voice view) and puts the pack right after the leading
  `system` or `developer` messages and the turn block at the end. The blocks it placed on the
  previous turn are taken out first, so the shared context never piles them up. A speculative
  inference gets them too, in its provisional copy.
- **Turns.** `observe()` subscribes to the aggregators: each user message written to the
  context (`on_user_turn_message_added`, final in cascade and realtime modes) is the customer's
  turn and each finished assistant turn (`on_assistant_turn_stopped`) is the agent's. `EndFrame`
  and `CancelFrame` end the conversation.
- **Tools.** `history_tools()` gives the three history tools as `FunctionSchema`s that carry their
  handlers, so the LLM service registers them from the context. Their JSON Schemas are the kit's.
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
    from pipecat.frames.frames import CancelFrame, EndFrame, Frame, LLMContextFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("Pipecat is not installed: pip install 'niadra[pipecat]'") from exc

from niadra._async_client import AsyncNiadra
from niadra.conversation import AsyncConversation
from niadra.integrations._common import (
    AnyKit,
    agent_turn,
    call_tool,
    customer_turn,
    end,
    handoff,
    instruction_count,
    kit_of,
    mark_injected,
    phone_or_none,
    read_context,
    tool_specs,
    verify_attestation,
    warn,
)
from niadra.models.common import Handle

__all__ = ["NiadraMemoryProcessor", "conversation_for_call", "history_tools"]


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


def history_tools(conversation: AsyncConversation) -> list[Any]:
    """The history tools as `FunctionSchema`s with their handlers, bound to the customer."""
    kit = kit_of(conversation)
    if kit is None:
        return []
    return [_HistorySchema(s.name, s.description, s.parameters, _handler(kit, s.name)) for s in tool_specs()]


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
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.conversation = conversation
        self.attestation = attestation
        self.role = role
        self._verified = False
        self._placed: list[dict[str, Any]] = []

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
        pack = await read_context(self.conversation)
        placed: list[dict[str, Any]] = []
        if pack is not None and pack.system_block:
            block = {"role": self.role, "content": pack.system_block}
            messages.insert(instruction_count(messages), block)
            placed.append(block)
        if pack is not None and pack.turn_block:
            block = {"role": self.role, "content": pack.turn_block}
            messages.append(block)
            placed.append(block)
        context.set_messages(messages)
        if pack is not None:
            mark_injected(self.conversation, pack)
        # A speculative inference runs on a provisional copy: the shared context keeps what it had.
        if not speculative:
            self._placed = placed
        else:
            self._placed = [*self._placed, *placed]

    async def _on_user_message(self, _aggregator: Any, message: Any) -> None:
        customer_turn(self.conversation, getattr(message, "content", None))

    async def _on_assistant_turn(self, _aggregator: Any, message: Any) -> None:
        agent_turn(self.conversation, getattr(message, "content", None))
