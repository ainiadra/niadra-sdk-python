"""Conversations and tasks: one customer, one thread of turns, one pinned pack.

`client.conversation()` and `client.task()` return context managers. Inside them:

- `context()` asks for the pack with the conversation's (or task's) id. The server pins the
  same bytes for every turn of it, and the SDK's cache answers most turns without a request.
  After the first pack, each read also asks for the delta: what changed since this agent last
  looked. Deltas are kept, in order, for as long as the pack stays the same, because the
  server sends each one only once; `turn_block` carries them with the live turns, for the end
  of the prompt. A new pack (after `verify()`, say) already includes them, so they are dropped.
- `customer()`, `agent()` and `human_agent()` record turns, and `action()` records what an
  agent did in a system of record. None of them block.
- Leaving the block emits `conversation.ended` (or `task.ended`), even when the block raised.

`mark_injected()` records the moment the pack went into the model's prompt. The agent's later
turns and actions carry it as `context_stamp`, with the pack's etag, which is how measurement
tells a context that arrived after the agent spoke from one it had and did not use. `wrap()`
calls it for you; without it, call it when you build the prompt.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, Union

from niadra._base import HandleLike, ItemLike, ObjectLike, TargetLike, VerificationLike, as_handle, as_object
from niadra._ids import new_key
from niadra.models.events import (
    BatchResponse,
    Content,
    ContextStamp,
    ConversationEndedItem,
    EventItem,
    SpeakerRef,
    TaskEndedItem,
    VerifyMethod,
)
from niadra.models.results import Context
from niadra.tools import BUILTIN_DEFINITIONS, AsyncToolKit, ToolKit
from niadra.vocabulary import Speaker, Verification

if TYPE_CHECKING:
    from niadra._async_client import AsyncNiadra
    from niadra._client import Niadra

AnySession = Union["Conversation", "Task", "AsyncConversation", "AsyncTask"]

_current: ContextVar[AnySession | None] = ContextVar("niadra_session", default=None)

T = TypeVar("T")


class _Fail(Protocol):
    def __call__(self, method: str, error: BaseException, default: T) -> T: ...


# Bounds the turn block of a very long conversation; the oldest deltas go first.
MAX_DELTAS = 20


def current_session() -> AnySession | None:
    """The conversation or task whose `with` block is running in this thread or task, if any."""
    return _current.get()


class _Session:
    _kind: Literal["conversation", "task"]

    def __init__(
        self,
        track: Callable[[ItemLike], bool],
        fail: _Fail,
        session_id: str | None,
        *,
        subject: HandleLike | None,
        object: ObjectLike | None,
        about: HandleLike | None,
        channel: str | None,
        view: str,
        verification: Verification | str,
        target: TargetLike | None,
        agent_id: str | None,
    ) -> None:
        self.id = session_id or new_key()
        self.subject = as_handle(subject) if subject is not None else None
        self.object = as_object(object) if object is not None else None
        self.about = as_handle(about) if about is not None else None
        self.channel = channel
        self.view = view
        self.verification = Verification(verification)
        self.target = target
        self.agent_id = agent_id
        self.context_injected_at: datetime | None = None
        self.first_agent_turn_at: datetime | None = None
        self.context_stamp: ContextStamp | None = None
        self.last_context: Context | None = None
        self._etag: str | None = None
        self._deltas: list[str] = []
        self._track = track
        self._fail = fail
        self._ended = False
        self._token: Token[AnySession | None] | None = None

    @property
    def conversation_id(self) -> str | None:
        return self.id if self._kind == "conversation" else None

    @property
    def task_id(self) -> str | None:
        return self.id if self._kind == "task" else None

    def customer(self, text: str, **event: Any) -> bool:
        """Records something the customer said. Extra keyword arguments go to the `EventItem`."""
        return self._turn(Speaker.CUSTOMER, "inbound", text, event)

    def agent(self, text: str, **event: Any) -> bool:
        """Records the AI agent's answer, stamped with the context its prompt carried."""
        if self.first_agent_turn_at is None:
            self.first_agent_turn_at = datetime.now(timezone.utc)
        if self.context_stamp is not None:
            event.setdefault("context_stamp", self.context_stamp)
        return self._turn(Speaker.AI_AGENT, "outbound", text, event)

    def human_agent(self, text: str, **event: Any) -> bool:
        """Records a turn by a human attendant."""
        return self._turn(Speaker.HUMAN_AGENT, "outbound", text, event)

    def mark_injected(self, context: Context | None = None, *, at: datetime | None = None) -> None:
        """Records that `context` (by default the last one this session returned) went into the prompt.

        Call it each time you build the prompt. The agent's next turns and actions carry the
        stamp; `context_injected_at` keeps the first injection of the session.
        """
        context = context if context is not None else self.last_context
        now = at or datetime.now(timezone.utc)
        if self.context_injected_at is None:
            self.context_injected_at = now
        etag = context.etag if context is not None and context.etag else None
        self.context_stamp = ContextStamp(etag=etag, injected_at=now)

    def _action_arguments(self, options: dict[str, Any]) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "subject": self.subject,
            "object": self.object,
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            "channel": self.channel,
            "speaker_id": self.agent_id,
        }
        # Only the AI agent acted on the injected context; a human's action carries no stamp.
        if self.context_stamp is not None and options.get("speaker", Speaker.AI_AGENT) == Speaker.AI_AGENT:
            defaults["context_stamp"] = self.context_stamp
        return {**defaults, **options}

    def _context_arguments(self) -> dict[str, Any]:
        # A task centers its pack on the object it works on; a conversation, on the person.
        by_object = self.object is not None and (self._kind == "task" or self.subject is None)
        return {
            "subject": None if by_object else self.subject,
            "object": self.object if by_object else None,
            "about": self.about,
            "view": self.view,
            "verification": self.verification,
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            "target": self.target,
            "delta": self._etag is not None,
        }

    def _is_pinned(self, overrides: dict[str, Any]) -> bool:
        # A read with a query is compiled for that query and never pinned, so it leaves the
        # conversation's pack and deltas alone.
        return not overrides.get("query")

    def _absorb(self, context: Context) -> Context:
        """Keeps the deltas received against the current pack and returns them all with it."""
        if context.origin == "empty":
            # Nothing is being served, not even the last good pack (a 401 or 403 lands here):
            # deltas mean nothing without the pack they are relative to.
            self._etag, self._deltas = None, []
        elif context.etag != self._etag:
            # A new pack includes what the old deltas said, and so does the delta sent with it.
            self._etag, self._deltas = context.etag, []
        elif context.delta and context.delta not in self._deltas:
            self._deltas = [*self._deltas, context.delta][-MAX_DELTAS:]
        result = context.model_copy(update={"delta": "\n\n".join(self._deltas) or None})
        self.last_context = result
        return result

    def _kit_binding(self) -> dict[str, Any] | None:
        if self.subject is None:
            return None
        return {
            "subject": self.subject,
            "about": self.about,
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            # Read at each call, so a kit made before `verify()` uses the new level.
            "verification": lambda: self.verification,
            "voice": self.view == "voice",
        }

    def _verified(self, level: VerificationLike) -> None:
        # The server pins a new pack for the new level; the next read starts from it.
        self.verification = Verification(level)
        self._etag, self._deltas = None, []

    def _turn(
        self, speaker: Speaker, direction: Literal["inbound", "outbound"], text: str, event: dict[str, Any]
    ) -> bool:
        try:
            if self.channel is None:
                raise ValueError("no channel: pass channel= to conversation() or to the client")
            item = EventItem(
                channel=self.channel,
                conversation_id=self.conversation_id,
                task_id=self.task_id,
                handles=[self.subject] if self.subject is not None else [],
                object_refs=[self.object] if self.object is not None else [],
                speaker=SpeakerRef(
                    role=speaker, id=self.agent_id if speaker is not Speaker.CUSTOMER else None
                ),
                direction=direction,
                content=Content(text=text),
                **event,
            )
        except (TypeError, ValueError) as exc:
            return self._fail(f"{speaker.value} turn", exc, False)
        return self._track(item)

    def _end_item(self) -> ItemLike:
        if self._kind == "conversation":
            return ConversationEndedItem(conversation_id=self.id)
        return TaskEndedItem(task_id=self.id)

    def end(self) -> bool:
        """Emits `conversation.ended` or `task.ended`. Later calls do nothing."""
        if self._ended:
            return False
        self._ended = True
        return self._track(self._end_item())

    def _enter(self) -> None:
        self._token = _current.set(self)  # type: ignore[arg-type]

    def _exit(self) -> None:
        if self._token is not None:
            try:
                _current.reset(self._token)
            except ValueError:
                _current.set(None)  # exited from another context; nothing better to restore
            self._token = None
        self.end()


class _SyncSession(_Session):
    def __init__(self, client: Niadra, session_id: str | None, **options: Any) -> None:
        super().__init__(client.track, client._core.fail, session_id, **options)
        self._client = client

    def context(self, **overrides: Any) -> Context:
        """The pack for this turn: the pinned bytes, with every delta since the pin in `delta`.

        Keyword arguments override the session's, e.g. `query=` for a one-off focused read.
        """
        context = self._client.context(**{**self._context_arguments(), **overrides})
        return self._absorb(context) if self._is_pinned(overrides) else context

    def action(self, operation: str, **options: Any) -> bool:
        """Records an action, defaulting subject, object, channel, ids and context stamp to this session's."""
        return self._client.action(operation, **self._action_arguments(options))

    def tools(self) -> ToolKit | None:
        """The history kit bound to this session's customer and id; None without a subject.

        The kit follows the session's verification level, so one made before `verify()`
        reads at the new level afterwards. Voice views get the voice budgets.
        """
        binding = self._kit_binding()
        return ToolKit(self._client, BUILTIN_DEFINITIONS, **binding) if binding is not None else None

    def verify(
        self,
        method: VerifyMethod,
        level: VerificationLike,
        *,
        handle: HandleLike | None = None,
        valid_until: datetime | None = None,
    ) -> BatchResponse | None:
        """Records that the customer proved who they are, and reads at the new level from then on.

        `handle` defaults to the session's subject. The level only rises when the server
        acknowledged the assertion.
        """
        handle = handle if handle is not None else self.subject
        if handle is None:
            return self._fail("verify", ValueError("pass handle=: this session has no subject"), None)
        result = self._client.verify(
            method,
            level,
            handle=handle,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            valid_until=valid_until,
        )
        if result is not None:
            self._verified(level)
        return result

    def __enter__(self) -> _SyncSession:
        self._enter()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._exit()


class _AsyncSession(_Session):
    def __init__(self, client: AsyncNiadra, session_id: str | None, **options: Any) -> None:
        super().__init__(client.track, client._core.fail, session_id, **options)
        self._client = client

    async def context(self, **overrides: Any) -> Context:
        """The pack for this turn: the pinned bytes, with every delta since the pin in `delta`."""
        context = await self._client.context(**{**self._context_arguments(), **overrides})
        return self._absorb(context) if self._is_pinned(overrides) else context

    def action(self, operation: str, **options: Any) -> bool:
        """Records an action, defaulting subject, object, channel, ids and context stamp to this session's."""
        return self._client.action(operation, **self._action_arguments(options))

    def tools(self) -> AsyncToolKit | None:
        """The history kit bound to this session's customer and id; None without a subject."""
        binding = self._kit_binding()
        return AsyncToolKit(self._client, BUILTIN_DEFINITIONS, **binding) if binding is not None else None

    async def verify(
        self,
        method: VerifyMethod,
        level: VerificationLike,
        *,
        handle: HandleLike | None = None,
        valid_until: datetime | None = None,
    ) -> BatchResponse | None:
        """Records that the customer proved who they are, and reads at the new level from then on."""
        handle = handle if handle is not None else self.subject
        if handle is None:
            return self._fail("verify", ValueError("pass handle=: this session has no subject"), None)
        result = await self._client.verify(
            method,
            level,
            handle=handle,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            valid_until=valid_until,
        )
        if result is not None:
            self._verified(level)
        return result

    async def __aenter__(self) -> _AsyncSession:
        self._enter()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._exit()


class Conversation(_SyncSession):
    _kind = "conversation"

    def handoff(self, target: Literal["human", "agent"], **options: Any) -> bool:
        """Records a transfer of this conversation to a human or another agent."""
        return self._client.handoff(self.id, target, **options)

    def __enter__(self) -> Conversation:
        self._enter()
        return self


class Task(_SyncSession):
    _kind = "task"

    def __enter__(self) -> Task:
        self._enter()
        return self


class AsyncConversation(_AsyncSession):
    _kind = "conversation"

    def handoff(self, target: Literal["human", "agent"], **options: Any) -> bool:
        """Records a transfer of this conversation to a human or another agent."""
        return self._client.handoff(self.id, target, **options)

    async def __aenter__(self) -> AsyncConversation:
        self._enter()
        return self


class AsyncTask(_AsyncSession):
    _kind = "task"

    async def __aenter__(self) -> AsyncTask:
        self._enter()
        return self


__all__ = ["AsyncConversation", "AsyncTask", "Conversation", "Task", "current_session"]
