"""LiveKit Agents (Python): the customer's memory in a voice agent, on the hooks LiveKit gives.

```python
from livekit.agents import AgentSession, JobContext
from niadra import AsyncNiadra
from niadra.integrations.livekit import NiadraAgent, conversation_for

niadra = AsyncNiadra(channel="voice")


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    caller = await ctx.wait_for_participant()
    conversation = conversation_for(niadra, caller, room=ctx.room)
    session = AgentSession(stt=..., llm=..., tts=...)
    await session.start(NiadraAgent(conversation, instructions="You are Acme's agent."), room=ctx.room)
```

What `NiadraAgent` (or the `NiadraMemory` mixin on your own `Agent` class) does:

- **Context.** Before each model call it reads `context()` (150 ms for the voice view) and puts
  the pack right after the agent's instructions and the turn block at the end. In a pipeline
  agent this happens in `llm_node`, on a copy of the chat context, so nothing piles up in the
  agent's history and LiveKit's preemptive generation still matches. With a realtime model it
  happens in `on_user_turn_completed`, on the turn's temporary context, which LiveKit syncs.
- **Turns.** The session's `conversation_item_added` events record each final user transcript
  (with its confidence) and each assistant message (with the usage the model reported).
  The session's `close` ends the conversation.
- **Tools.** The three history tools, as raw function tools with the kit's own schemas, bound to
  the caller. Pass `history_tools=False` to leave them out.
- **Verification.** `attestation=` is the carrier's STIR/SHAKEN level (`A`, `B` or `C`), which
  LiveKit does not read itself: map the SIP header to a participant attribute and pass it. It is
  verified once, before the first context.
- **Handoff.** When the session moves to another agent, `handoff("agent")` is recorded; give the
  next `NiadraAgent` the same conversation. Call `transferred_to_human()` when you transfer the
  call to a person (SIP transfer or warm transfer).

Nothing here can fail a turn: a context that cannot be read is left out, and a failure to
record is logged without content.
"""

from __future__ import annotations

import hashlib
import inspect
import weakref
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

try:
    from livekit.agents import Agent, AgentSession, ModelSettings, function_tool, llm
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError(
        "niadra.integrations.livekit needs LiveKit Agents: pip install 'niadra[livekit]'"
    ) from exc

from niadra._async_client import AsyncNiadra
from niadra.conversation import AsyncConversation
from niadra.handles import app_user
from niadra.integrations._common import (
    MARK,
    agent_turn,
    call_tool,
    customer_turn,
    end,
    handoff,
    instruction_count,
    kit_of,
    mark_injected,
    model_usage,
    phone_or_none,
    read_context,
    tool_specs,
    verify_attestation,
    warn,
)
from niadra.models.common import Handle
from niadra.models.events import ModelUsage
from niadra.models.results import Context

__all__ = ["NiadraAgent", "NiadraMemory", "conversation_for", "history_tools"]

SIP_PHONE = "sip.phoneNumber"
SIP_CALL_ID = "sip.callID"


def conversation_for(
    niadra: AsyncNiadra,
    participant: Any,
    *,
    room: Any = None,
    subject: Handle | Callable[[Any], Handle | None] | None = None,
    conversation_id: str | None = None,
    channel: str = "voice",
    view: str = "voice",
    agent_id: str | None = None,
) -> AsyncConversation:
    """The Niadra conversation of a LiveKit call, from the remote participant.

    The subject is the caller's number (`sip.phoneNumber`) on a SIP call and the participant's
    identity as an app user otherwise; pass `subject=` (a handle, or a function of the
    participant) when your identities mean something else. The conversation id is the SIP call
    id, else the room's name.
    """
    attributes: Mapping[str, str] = getattr(participant, "attributes", None) or {}
    if callable(subject):
        handle = subject(participant)
    elif subject is not None:
        handle = subject
    else:
        handle = phone_or_none(attributes.get(SIP_PHONE))
        identity = getattr(participant, "identity", None)
        if handle is None and identity:
            handle = app_user(str(identity))
    call_id = conversation_id or attributes.get(SIP_CALL_ID) or getattr(room, "name", None) or None
    return niadra.conversation(call_id, subject=handle, channel=channel, view=view, agent_id=agent_id)


def history_tools(conversation: AsyncConversation) -> list[Any]:
    """The history tools as LiveKit raw function tools bound to the conversation's customer."""
    kit = kit_of(conversation)
    if kit is None:
        return []
    tools: list[Any] = []
    for spec in tool_specs(kit.definitions):

        def bind(name: str) -> Callable[..., Any]:
            async def run(raw_arguments: dict[str, object]) -> str:
                return await call_tool(kit, name, raw_arguments)

            return run

        schema = {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
        tools.append(function_tool(bind(spec.name), raw_schema=schema))
    return tools


@dataclass(eq=False)
class _Call:
    """What every agent of one call shares: one verification, one set of listeners, the last usage."""

    attestation: str | None = None
    verified: bool = False
    sessions: weakref.WeakSet[Any] = field(default_factory=weakref.WeakSet)
    usage: ModelUsage | None = None


_CALLS: weakref.WeakKeyDictionary[AsyncConversation, _Call] = weakref.WeakKeyDictionary()


def _call(conversation: AsyncConversation, attestation: str | None) -> _Call:
    call = _CALLS.get(conversation)
    if call is None:
        call = _CALLS[conversation] = _Call(attestation=attestation)
    elif attestation and not call.attestation:
        call.attestation = attestation
    return call


def _message_id(kind: str, text: str) -> str:
    # A stable id per content: a realtime session syncs by id, and the pinned pack keeps its id.
    return f"{MARK}_{kind}_{hashlib.sha256(text.encode()).hexdigest()[:16]}"


def _is_marked(chat_ctx: Any) -> bool:
    return any(getattr(item, "extra", None) and MARK in item.extra for item in chat_ctx.items)


def _role(item: Any) -> Any:
    return getattr(item, "role", None) if getattr(item, "type", None) == "message" else None


def _place(chat_ctx: Any, context: Context) -> None:
    """Puts the pack after the leading instructions and the turn block at the end of `chat_ctx`."""
    items = chat_ctx.items
    if context.system_block:
        message = llm.ChatMessage(
            id=_message_id("context", context.system_block),
            role="system",
            content=[context.system_block],
            extra={MARK: "context"},
        )
        items.insert(instruction_count(items, role=_role), message)
    if context.turn_block:
        items.append(
            llm.ChatMessage(
                id=_message_id("turn", context.turn_block),
                role="system",
                content=[context.turn_block],
                extra={MARK: "turn"},
            )
        )


class NiadraMemory:
    """Mixin for a LiveKit `Agent`: `class Receptionist(NiadraMemory, Agent)`.

    Takes `conversation` (from `conversation_for()` or `AsyncNiadra.conversation()`), an optional
    `attestation`, and `history_tools` (default True); everything else goes to `Agent`. If you
    override `on_enter`, `on_user_turn_completed` or `llm_node`, call `super()`.
    """

    def __init__(
        self,
        *args: Any,
        conversation: AsyncConversation,
        attestation: str | None = None,
        history_tools: bool = True,
        **kwargs: Any,
    ) -> None:
        tools = list(kwargs.pop("tools", None) or [])
        if history_tools:
            tools.extend(_history_tools(conversation))
        super().__init__(*args, tools=tools, **kwargs)  # type: ignore[call-arg]
        self.niadra = conversation
        self._niadra_call = _call(conversation, attestation)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the call went to a person. Call it where your code transfers the call."""
        handoff(self.niadra, "human", reason=reason)

    async def on_enter(self) -> None:
        self._niadra_listen(self._niadra_session())
        await super().on_enter()  # type: ignore[misc]

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        # A pipeline agent gets the context in `llm_node`; changing this context would also make
        # LiveKit throw away a preemptive generation. A realtime model has no `llm_node`.
        if self._niadra_realtime() and not _is_marked(turn_ctx):
            context = await self._niadra_context()
            if context is not None:
                _place(turn_ctx, context)
                mark_injected(self.niadra, context)
        await super().on_user_turn_completed(turn_ctx, new_message)  # type: ignore[misc]

    async def llm_node(
        self, chat_ctx: Any, tools: list[Any], model_settings: ModelSettings
    ) -> AsyncIterator[Any]:
        if not _is_marked(chat_ctx):
            context = await self._niadra_context()
            if context is not None:
                chat_ctx = chat_ctx.copy()
                _place(chat_ctx, context)
                mark_injected(self.niadra, context)
        output = super().llm_node(chat_ctx, tools, model_settings)  # type: ignore[misc]
        if inspect.isawaitable(output):
            output = await output
        if output is None:
            return
        if not isinstance(output, AsyncIterable):
            yield output
            return
        async for chunk in output:
            if isinstance(chunk, llm.ChatChunk) and chunk.usage is not None:
                self._niadra_usage(chunk.usage)
            yield chunk

    async def _niadra_context(self) -> Context | None:
        call = self._niadra_call
        if not call.verified:
            call.verified = True
            await verify_attestation(self.niadra, call.attestation)
        return await read_context(self.niadra)

    def _niadra_session(self) -> Any:
        try:
            return self.session  # type: ignore[attr-defined]
        except Exception:
            return None

    def _niadra_realtime(self) -> bool:
        model = getattr(self, "llm", None)
        if not isinstance(model, (llm.LLM, llm.RealtimeModel)):
            model = getattr(self._niadra_session(), "llm", None)
        return isinstance(model, llm.RealtimeModel)

    def _niadra_usage(self, usage: Any) -> None:
        try:
            model = getattr(self, "llm", None)
            if not isinstance(model, llm.LLM):
                model = getattr(self._niadra_session(), "llm", None)
            self._niadra_call.usage = model_usage(
                getattr(model, "provider", None),
                getattr(model, "model", None),
                usage.prompt_tokens,
                usage.prompt_cached_tokens or usage.cache_read_tokens,
                usage.cache_creation_tokens,
            )
        except Exception as exc:
            warn("read the model's usage", exc)

    def _niadra_listen(self, session: Any) -> None:
        call = self._niadra_call
        if not isinstance(session, AgentSession) or session in call.sessions:
            return
        call.sessions.add(session)
        conversation = self.niadra

        def on_item(event: Any) -> None:
            item = event.item
            if getattr(item, "type", None) == "agent_handoff":
                if item.old_agent_id is not None:
                    handoff(conversation, "agent")
                return
            if getattr(item, "type", None) != "message" or MARK in (item.extra or {}):
                return
            if item.role == "user":
                confidence = item.transcript_confidence
                valid = isinstance(confidence, float) and 0 <= confidence <= 1
                customer_turn(conversation, item.text_content, stt_confidence=confidence if valid else None)
            elif item.role == "assistant":
                usage, call.usage = call.usage, None
                agent_turn(conversation, item.text_content, usage=usage)

        session.on("conversation_item_added", on_item)
        session.on("close", lambda _event: end(conversation))


def _history_tools(conversation: AsyncConversation) -> list[Any]:
    try:
        return history_tools(conversation)
    except Exception as exc:
        warn("build the history tools", exc)
        return []


class NiadraAgent(NiadraMemory, Agent):  # type: ignore[misc]
    """A LiveKit `Agent` with the customer's memory. `NiadraAgent(conversation, instructions=...)`."""

    def __init__(self, conversation: AsyncConversation, **kwargs: Any) -> None:
        super().__init__(conversation=conversation, **kwargs)
