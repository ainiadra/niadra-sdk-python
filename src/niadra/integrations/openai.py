"""`wrap()` for the OpenAI Python client and any client with the same shape.

```python
from openai import OpenAI
from niadra import Niadra, phone, wrap

niadra = Niadra(channel="whatsapp")
openai = wrap(OpenAI())

with niadra.conversation(thread_id, subject=phone("+5511912345678")) as conversation:
    conversation.customer(incoming_text)
    reply = openai.chat.completions.create(model="gpt-4.1", messages=messages)
```

Inside a conversation or task block, `chat.completions.create` and `chat.completions.parse`
(sync or async, streaming or not, and through `with_raw_response`) get the pinned pack as a
system message right after your own leading system messages, and the turn block (deltas and
live turns from other channels) as a system message at the end. The injection is stamped on
the session, and the model's answer is recorded as an `ai_agent` turn carrying that stamp.
Outside a block, calls pass through untouched.

The pack goes after your instructions, not before them, because your instructions are the
same for every customer: kept first, they stay the cacheable prefix of the prompt.

Through `with_raw_response`, the answer is recorded when you call `parse()` on the raw
response. `with_streaming_response` is not intercepted.

The wrapper returns a proxy and never modifies the client you pass in. It makes no network
calls of its own besides Niadra's (the context, served from the cache on most turns). Nothing
it does can fail your model call: a context that cannot be fetched is left out, and a failure
to record the answer is logged, without content, and swallowed.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from typing import Any, TypeVar, cast

from niadra.conversation import AnySession, current_session
from niadra.conversation import _AsyncSession as AsyncSession
from niadra.conversation import _SyncSession as SyncSession
from niadra.models.results import Context

logger = logging.getLogger("niadra")

C = TypeVar("C")

_INSTRUCTION_ROLES = frozenset({"system", "developer"})
_INTERCEPTED = ("create", "parse")


def wrap(client: C, *, conversation: AnySession | None = None) -> C:
    """Returns a proxy of an `OpenAI` or `AsyncOpenAI` client that injects context and records answers.

    By default the conversation is the one whose `with` block is running; pass `conversation=`
    to bind the proxy to one explicitly.
    """
    overrides: dict[str, Any] = {"chat": _wrap_chat(client.chat, conversation)}  # type: ignore[attr-defined]
    beta_chat = getattr(getattr(client, "beta", None), "chat", None)
    if getattr(beta_chat, "completions", None) is not None:
        # Older clients keep structured outputs under `beta.chat.completions.parse`.
        overrides["beta"] = _Proxy(client.beta, {"chat": _wrap_chat(beta_chat, conversation)})  # type: ignore[attr-defined]
    return cast(C, _Proxy(client, overrides))


def _wrap_chat(chat: Any, explicit: AnySession | None) -> _Proxy:
    completions = chat.completions
    overrides: dict[str, Any] = {
        name: _intercept(getattr(completions, name), explicit, raw=False)
        for name in _INTERCEPTED
        if callable(getattr(completions, name, None))
    }
    raw = getattr(completions, "with_raw_response", None)
    if raw is not None:
        overrides["with_raw_response"] = _Proxy(
            raw,
            {
                name: _intercept(getattr(raw, name), explicit, raw=True)
                for name in _INTERCEPTED
                if callable(getattr(raw, name, None))
            },
        )
    return _Proxy(chat, {"completions": _Proxy(completions, overrides)})


class _Proxy:
    """Delegates every attribute to `target`, except the ones in `overrides`."""

    def __init__(self, target: Any, overrides: dict[str, Any]) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_target"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_target"), name, value)

    def __repr__(self) -> str:
        return f"niadra.wrap({object.__getattribute__(self, '_target')!r})"


def inject(context: Context, messages: Sequence[Any]) -> list[Any]:
    """Places the pack after the leading system or developer messages and the turn block at the end."""
    result = list(messages)
    if context.system_block:
        position = 0
        while position < len(result) and _role(result[position]) in _INSTRUCTION_ROLES:
            position += 1
        result.insert(position, {"role": "system", "content": context.system_block})
    if context.turn_block:
        result.append({"role": "system", "content": context.turn_block})
    return result


def _role(message: Any) -> Any:
    if isinstance(message, Mapping):
        return message.get("role")
    return getattr(message, "role", None)


def _is_async(method: Any) -> bool:
    # The OpenAI client decorates its async methods with plain wrappers, which hide the
    # coroutine function from a direct check; `__wrapped__` leads back to it.
    return inspect.iscoroutinefunction(method) or inspect.iscoroutinefunction(inspect.unwrap(method))


def _session(explicit: AnySession | None, expected: type) -> AnySession | None:
    session = explicit or current_session()
    if session is None:
        return None
    if not isinstance(session, expected):
        logger.warning(
            "niadra: wrap() skipped a %s conversation on a client of the other kind", type(session).__name__
        )
        return None
    return session


def _prepare(session: AnySession, context: Context, kwargs: dict[str, Any]) -> None:
    messages = kwargs.get("messages")
    if messages is None:
        return
    kwargs["messages"] = inject(context, messages)
    # A holdout pack is empty on purpose, and the turn was still built with it.
    if context.system_block or context.turn_block or context.is_holdout:
        session.mark_injected(context)


def _intercept(original: Callable[..., Any], explicit: AnySession | None, *, raw: bool) -> Callable[..., Any]:
    if _is_async(original):

        async def call_async(*args: Any, **kwargs: Any) -> Any:
            session = _session(explicit, AsyncSession)
            if session is None:
                return await original(*args, **kwargs)
            assert isinstance(session, AsyncSession)
            try:
                _prepare(session, await session.context(), kwargs)
            except Exception as exc:
                logger.warning("niadra: could not inject context (%s)", type(exc).__name__)
            result = await original(*args, **kwargs)
            return _Answer(session, bool(kwargs.get("stream")), streams_async=True).take(result, raw=raw)

        return call_async

    def call(*args: Any, **kwargs: Any) -> Any:
        session = _session(explicit, SyncSession)
        if session is None:
            return original(*args, **kwargs)
        assert isinstance(session, SyncSession)
        try:
            _prepare(session, session.context(), kwargs)
        except Exception as exc:
            logger.warning("niadra: could not inject context (%s)", type(exc).__name__)
        result = original(*args, **kwargs)
        return _Answer(session, bool(kwargs.get("stream")), streams_async=False).take(result, raw=raw)

    return call


class _Answer:
    """The capture of one model call: records its text once, whichever way the caller reads it."""

    def __init__(self, session: AnySession, stream: bool, *, streams_async: bool) -> None:
        self._session = session
        self._stream = stream
        self._streams_async = streams_async
        self._recorded = False

    def take(self, result: Any, *, raw: bool) -> Any:
        """What to hand back to the caller in place of `result`. Never raises."""
        try:
            if raw:
                return _RawResponse(result, self)
            return self.parsed(result)
        except Exception as exc:
            logger.warning("niadra: could not capture the model's answer (%s)", type(exc).__name__)
            return result

    def parsed(self, result: Any) -> Any:
        if not self._stream:
            self.record(_answer(result))
            return result
        if self._streams_async:
            return _AsyncStream(result, self)
        return _SyncStream(result, self)

    def record(self, text: str | None) -> None:
        if self._recorded or not text:
            return
        self._recorded = True
        try:
            self._session.agent(text)
        except Exception as exc:
            logger.warning("niadra: could not record the model's answer (%s)", type(exc).__name__)


class _RawResponse(_Proxy):
    """A raw response whose `parse()` hands back the captured answer."""

    def __init__(self, target: Any, answer: _Answer) -> None:
        super().__init__(target, {"parse": self._parse})
        object.__setattr__(self, "_answer", answer)

    def _parse(self, *args: Any, **kwargs: Any) -> Any:
        target = object.__getattribute__(self, "_target")
        answer: _Answer = object.__getattribute__(self, "_answer")
        parsed = target.parse(*args, **kwargs)
        if inspect.isawaitable(parsed):
            return _parse_later(parsed, answer)
        return _guarded(answer, parsed)


async def _parse_later(parsed: Awaitable[Any], answer: _Answer) -> Any:
    return _guarded(answer, await parsed)


def _guarded(answer: _Answer, parsed: Any) -> Any:
    try:
        return answer.parsed(parsed)
    except Exception as exc:
        logger.warning("niadra: could not capture the model's answer (%s)", type(exc).__name__)
        return parsed


def _first_choice(value: Any) -> Any:
    # With `n > 1` the model answers several times; the agent said the first one.
    for choice in getattr(value, "choices", None) or ():
        if getattr(choice, "index", 0) in (0, None):
            return choice
    return None


def _answer(response: Any) -> str | None:
    content = getattr(getattr(_first_choice(response), "message", None), "content", None)
    return content if isinstance(content, str) and content else None


def _delta_text(chunk: Any) -> str:
    try:
        content = getattr(getattr(_first_choice(chunk), "delta", None), "content", None)
    except Exception:
        return ""
    return content if isinstance(content, str) else ""


class _SyncStream:
    """Passes chunks through and records the assembled answer when the stream ends or is closed."""

    def __init__(self, stream: Any, answer: _Answer) -> None:
        self._stream = stream
        self._answer = answer
        self._parts: list[str] = []
        self._chunks: Iterator[Any] | None = None

    def __iter__(self) -> Iterator[Any]:
        # One pass-through generator, so `next()` and a `for` loop continue from the same place.
        if self._chunks is None:
            self._chunks = self._pass_through()
        return self._chunks

    def __next__(self) -> Any:
        return next(iter(self))

    def _pass_through(self) -> Iterator[Any]:
        try:
            for chunk in self._stream:
                self._parts.append(_delta_text(chunk))
                yield chunk
        finally:
            self._finish()

    def __enter__(self) -> _SyncStream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._finish()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def _finish(self) -> None:
        self._answer.record("".join(self._parts))


class _AsyncStream:
    """The async twin of `_SyncStream`."""

    def __init__(self, stream: Any, answer: _Answer) -> None:
        self._stream = stream
        self._answer = answer
        self._parts: list[str] = []
        self._chunks: AsyncIterator[Any] | None = None

    def __aiter__(self) -> AsyncIterator[Any]:
        if self._chunks is None:
            self._chunks = self._pass_through()
        return self._chunks

    async def __anext__(self) -> Any:
        return await self.__aiter__().__anext__()

    async def _pass_through(self) -> AsyncIterator[Any]:
        try:
            async for chunk in self._stream:
                self._parts.append(_delta_text(chunk))
                yield chunk
        finally:
            self._finish()

    async def __aenter__(self) -> _AsyncStream:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            await self._stream.close()
        finally:
            self._finish()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def _finish(self) -> None:
        self._answer.record("".join(self._parts))
