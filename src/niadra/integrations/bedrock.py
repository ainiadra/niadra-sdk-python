"""`wrap()` for Amazon Bedrock's Converse API through boto3 (`bedrock-runtime`).

```python
import boto3
from niadra import Niadra, phone
from niadra.integrations.bedrock import wrap

niadra = Niadra(channel="chat")
bedrock = wrap(boto3.client("bedrock-runtime"))

with niadra.conversation(thread_id, subject=phone(caller)):
    response = bedrock.converse(modelId=MODEL_ID, system=[{"text": INSTRUCTIONS}], messages=history)
```

Inside a conversation or task block, `converse` and `converse_stream` get the pack (after the
agent's own notes, with `agent_memory=`) as a `system` text block after your own, followed by a
`cachePoint` only when your system blocks already use one, and the turn block as a text block at
the end of the last user message. The answer is recorded as the agent's turn with Bedrock's
usage: input tokens plus the cache reads and writes, and the `modelId`. Outside a block, calls
pass through untouched, and nothing the wrapper does can fail your call. Only the Converse calls
are intercepted; every other method is the client's own.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, TypeVar, cast

from niadra.conversation import _SyncSession as SyncSession
from niadra.integrations._common import (
    AgentMemoryLike,
    AgentMemoryOption,
    AnySession,
    Prompt,
    agent_turn,
    mark_injected,
    memory_option,
    model_usage,
    read_prompt,
    run_sync,
    warn,
)
from niadra.integrations.openai import _Proxy, _session

__all__ = ["inject", "wrap"]

C = TypeVar("C")


def wrap(client: C, *, conversation: AnySession | None = None, agent_memory: AgentMemoryLike = None) -> C:
    """A proxy of a boto3 `bedrock-runtime` client that injects the context and records the answers."""
    option = memory_option(agent_memory)
    overrides = {
        "converse": _intercept(client.converse, conversation, option, stream=False),  # type: ignore[attr-defined]
        "converse_stream": _intercept(client.converse_stream, conversation, option, stream=True),  # type: ignore[attr-defined]
    }
    return cast(C, _Proxy(client, overrides))


def inject(prompt: Prompt, kwargs: dict[str, Any]) -> dict[str, Any]:
    """The request's keyword arguments with the prompt placed; the caller's objects are not changed."""
    result = dict(kwargs)
    if prompt.system:
        system = list(kwargs.get("system") or [])
        blocks: list[dict[str, Any]] = [{"text": prompt.system}]
        if any(isinstance(b, Mapping) and "cachePoint" in b for b in system):
            blocks.append({"cachePoint": {"type": "default"}})
        result["system"] = [*system, *blocks]
    if prompt.turn:
        history = list(kwargs.get("messages") or [])
        data = {"text": prompt.turn}
        last = history[-1] if history else None
        if isinstance(last, Mapping) and last.get("role") == "user":
            history[-1] = {**last, "content": [*(last.get("content") or []), data]}
        else:
            history.append({"role": "user", "content": [data]})
        result["messages"] = history
    return result


def _prepared(
    session: AnySession, option: AgentMemoryOption | None, kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        prompt = run_sync(read_prompt(session, option))
        if not (prompt.system or prompt.turn):
            return kwargs
        placed = inject(prompt, kwargs)
        if prompt.context is not None:
            mark_injected(session, prompt.context)
        return placed
    except Exception as exc:
        warn("place the context", exc)
        return kwargs


def _usage(usage: Any, model: Any) -> Any:
    if not isinstance(usage, Mapping):
        return None
    read, written = usage.get("cacheReadInputTokens") or 0, usage.get("cacheWriteInputTokens") or 0
    inputs = usage.get("inputTokens")
    prompt = inputs + read + written if isinstance(inputs, int) else None
    return model_usage("bedrock", model if isinstance(model, str) else None, prompt, read, written)


def _record(session: AnySession, text: str, usage: Any) -> None:
    try:
        agent_turn(session, text, usage=usage)
    except Exception as exc:
        warn("record the model's answer", exc)


def _intercept(
    original: Any, explicit: AnySession | None, option: AgentMemoryOption | None, *, stream: bool
) -> Any:
    def call(*args: Any, **kwargs: Any) -> Any:
        session = _session(explicit, SyncSession)
        if session is None:
            return original(*args, **kwargs)
        response = original(*args, **_prepared(session, option, kwargs))
        try:
            if stream:
                return {**response, "stream": _Events(response["stream"], session, kwargs.get("modelId"))}
            message = ((response.get("output") or {}).get("message") or {}).get("content") or []
            text = "".join(b.get("text", "") for b in message if isinstance(b, Mapping))
            _record(session, text, _usage(response.get("usage"), kwargs.get("modelId")))
        except Exception as exc:
            warn("capture the model's answer", exc)
        return response

    return call


class _Events:
    """The Converse stream: passes its events through and records the answer when it ends."""

    def __init__(self, events: Any, session: AnySession, model: Any) -> None:
        self._events = events
        self._session = session
        self._model = model
        self._parts: list[str] = []
        self._usage: Any = None
        self._done = False

    def __iter__(self) -> Iterator[Any]:
        try:
            for event in self._events:
                if isinstance(event, Mapping):
                    delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
                    if isinstance(delta.get("text"), str):
                        self._parts.append(delta["text"])
                    if "metadata" in event:
                        self._usage = (event["metadata"] or {}).get("usage")
                yield event
        finally:
            self._finish()

    def _finish(self) -> None:
        if not self._done:
            self._done = True
            _record(self._session, "".join(self._parts), _usage(self._usage, self._model))

    def close(self) -> None:
        try:
            closer = getattr(self._events, "close", None)
            if callable(closer):
                closer()
        finally:
            self._finish()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._events, name)
