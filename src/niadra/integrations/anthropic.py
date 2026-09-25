"""`wrap()` for the Anthropic Python client (`Anthropic`, `AsyncAnthropic` and the Bedrock and
Vertex clients of the same SDK).

```python
from anthropic import Anthropic
from niadra import Niadra, phone
from niadra.integrations.anthropic import wrap

niadra = Niadra(channel="chat")
claude = wrap(Anthropic())

with niadra.conversation(thread_id, subject=phone(caller)):
    message = claude.messages.create(
        model="claude-sonnet-4-5", max_tokens=1024, system=INSTRUCTIONS, messages=history
    )
```

Inside a conversation or task block, `messages.create` (plain or `stream=True`) and
`messages.stream` get:

- the pack (after the agent's own notes, with `agent_memory=`) in `system`, as a text block after
  your own system text; it carries `cache_control` only when your system blocks already do, so
  it joins a prefix you chose to cache;
- the turn block (deltas and live turns from other channels) as a text block at the end of the
  last user message, tagged as data, or as a user message of its own when the last one is not the
  customer's.

The answer (its text blocks) is recorded as the agent's turn with Anthropic's usage: input tokens,
cache reads and cache writes. Outside a block, calls pass through untouched, and nothing the
wrapper does can fail your call.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any, TypeVar, cast

from niadra.conversation import _AsyncSession as AsyncSession
from niadra.conversation import _SyncSession as SyncSession
from niadra.integrations._common import (
    AgentMemoryLike,
    AgentMemoryOption,
    AnySession,
    Prompt,
    agent_turn,
    mark_injected,
    maybe_await,
    memory_option,
    read_prompt,
    run_sync,
    warn,
)
from niadra.integrations.openai import _is_async, _Proxy, _session
from niadra.models.events import ModelUsage

__all__ = ["inject", "wrap"]

C = TypeVar("C")


def wrap(client: C, *, conversation: AnySession | None = None, agent_memory: AgentMemoryLike = None) -> C:
    """A proxy of an Anthropic client that injects the context and records the answers."""
    option = memory_option(agent_memory)
    messages = client.messages  # type: ignore[attr-defined]
    overrides: dict[str, Any] = {"create": _create(messages.create, conversation, option)}
    if callable(getattr(messages, "stream", None)):
        overrides["stream"] = _stream(messages.stream, conversation, option)
    return cast(C, _Proxy(client, {"messages": _Proxy(messages, overrides)}))


def _has_cache_control(system: Any) -> bool:
    return isinstance(system, list) and any(isinstance(b, Mapping) and "cache_control" in b for b in system)


def inject(prompt: Prompt, kwargs: dict[str, Any]) -> dict[str, Any]:
    """The request's keyword arguments with the prompt placed; the caller's objects are not changed."""
    result = dict(kwargs)
    if prompt.system:
        system = kwargs.get("system")
        block: dict[str, Any] = {"type": "text", "text": prompt.system}
        if _has_cache_control(system):
            block["cache_control"] = {"type": "ephemeral"}
        if not system:
            result["system"] = prompt.system
        elif isinstance(system, str):
            result["system"] = [{"type": "text", "text": system}, block]
        else:
            result["system"] = [*system, block]
    if prompt.turn:
        history = list(kwargs.get("messages") or [])
        data = {"type": "text", "text": prompt.turn}
        last = history[-1] if history else None
        if isinstance(last, Mapping) and last.get("role") == "user":
            content = last.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
            history[-1] = {**last, "content": [*blocks, data]}
        else:
            history.append({"role": "user", "content": [data]})
        result["messages"] = history
    return result


async def _prepared(
    session: AnySession, option: AgentMemoryOption | None, kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        prompt = await read_prompt(session, option)
        if not (prompt.system or prompt.turn):
            return kwargs
        placed = inject(prompt, kwargs)
        if prompt.context is not None:
            mark_injected(session, prompt.context)
        return placed
    except Exception as exc:
        warn("place the context", exc)
        return kwargs


def _prepared_sync(
    session: AnySession, option: AgentMemoryOption | None, kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        return run_sync(_prepared(session, option, kwargs))
    except TypeError as exc:
        warn("place the context", exc)
        return kwargs


def _text(message: Any) -> str:
    parts = [
        getattr(b, "text", None)
        for b in getattr(message, "content", None) or []
        if getattr(b, "type", "") == "text"
    ]
    return "".join(p for p in parts if isinstance(p, str))


def _record(session: AnySession, message: Any) -> None:
    try:
        agent_turn(session, _text(message), usage=ModelUsage.from_response(message, provider="anthropic"))
    except Exception as exc:
        warn("record the model's answer", exc)


def _create(
    original: Callable[..., Any], explicit: AnySession | None, option: AgentMemoryOption | None
) -> Any:
    if _is_async(original):

        async def create_async(*args: Any, **kwargs: Any) -> Any:
            session = _session(explicit, AsyncSession)
            if session is None:
                return await original(*args, **kwargs)
            result = await original(*args, **await _prepared(session, option, kwargs))
            if kwargs.get("stream"):
                return _AsyncEvents(result, session)
            _record(session, result)
            return result

        return create_async

    def create(*args: Any, **kwargs: Any) -> Any:
        session = _session(explicit, SyncSession)
        if session is None:
            return original(*args, **kwargs)
        result = original(*args, **_prepared_sync(session, option, kwargs))
        if kwargs.get("stream"):
            return _Events(result, session)
        _record(session, result)
        return result

    return create


def _stream(
    original: Callable[..., Any], explicit: AnySession | None, option: AgentMemoryOption | None
) -> Any:
    def stream(*args: Any, **kwargs: Any) -> Any:
        sync_session = _session(explicit, SyncSession) if not _async_manager(original) else None
        if sync_session is not None:
            return _Manager(original(*args, **_prepared_sync(sync_session, option, kwargs)), sync_session)
        async_session = _session(explicit, AsyncSession) if _async_manager(original) else None
        if async_session is not None:
            return _AsyncManager(original, args, kwargs, async_session, option)
        return original(*args, **kwargs)

    return stream


def _async_manager(method: Any) -> bool:
    owner = getattr(method, "__self__", None)
    return "Async" in type(owner).__name__


class _Accumulator:
    """The answer of a raw event stream: its text deltas and the usage of `message_start`."""

    def __init__(self, session: AnySession) -> None:
        self.session = session
        self.parts: list[str] = []
        self.message: Any = None
        self.done = False

    def observe(self, event: Any) -> None:
        kind = getattr(event, "type", "")
        if kind == "message_start":
            self.message = getattr(event, "message", None)
        elif kind == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
            self.parts.append(event.delta.text)

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        try:
            usage = ModelUsage.from_response(self.message, provider="anthropic") if self.message else None
            agent_turn(self.session, "".join(self.parts), usage=usage)
        except Exception as exc:
            warn("record the model's answer", exc)


class _Events:
    def __init__(self, stream: Any, session: AnySession) -> None:
        self._stream = stream
        self._answer = _Accumulator(session)

    def __iter__(self) -> Iterator[Any]:
        try:
            for event in self._stream:
                self._answer.observe(event)
                yield event
        finally:
            self._answer.finish()

    def __enter__(self) -> _Events:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._answer.finish()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _AsyncEvents(_Events):
    async def __aiter__(self) -> Any:
        try:
            async for event in self._stream:
                self._answer.observe(event)
                yield event
        finally:
            self._answer.finish()

    async def __aenter__(self) -> _AsyncEvents:
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            await self._stream.close()
        finally:
            self._answer.finish()


class _Manager:
    """`messages.stream()`: records the message the stream built when the `with` block ends."""

    def __init__(self, manager: Any, session: AnySession) -> None:
        self._manager = manager
        self._session = session
        self._stream: Any = None

    def __enter__(self) -> Any:
        self._stream = self._manager.__enter__()
        return self._stream

    def __exit__(self, *exc: Any) -> Any:
        try:
            if self._stream is not None:
                _record(self._session, self._stream.current_message_snapshot)
        except Exception as exc_:
            warn("record the model's answer", exc_)
        return self._manager.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)


class _AsyncManager:
    """The async twin: the context is read when the `async with` block starts."""

    def __init__(
        self,
        original: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        session: AnySession,
        option: AgentMemoryOption | None,
    ) -> None:
        self._original = original
        self._args = args
        self._kwargs = kwargs
        self._session = session
        self._option = option
        self._manager: Any = None
        self._stream: Any = None

    async def __aenter__(self) -> Any:
        kwargs = await _prepared(self._session, self._option, self._kwargs)
        self._manager = self._original(*self._args, **kwargs)
        self._stream = await maybe_await(self._manager.__aenter__())
        return self._stream

    async def __aexit__(self, *exc: Any) -> Any:
        try:
            if self._stream is not None:
                _record(self._session, self._stream.current_message_snapshot)
        except Exception as exc_:
            warn("record the model's answer", exc_)
        return await self._manager.__aexit__(*exc)
