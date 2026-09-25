"""`wrap()` for the Google GenAI client (`google-genai`: the Gemini API and Vertex AI).

```python
from google import genai
from niadra import Niadra, phone
from niadra.integrations.google_genai import wrap

niadra = Niadra(channel="chat")
gemini = wrap(genai.Client())

with niadra.conversation(thread_id, subject=phone(caller)):
    response = gemini.models.generate_content(
        model="gemini-2.5-flash", contents=history, config={"system_instruction": INSTRUCTIONS}
    )
```

Inside a conversation or task block, `models.generate_content` and `generate_content_stream`
(and the same under `client.aio`) get the pack (after the agent's own notes, with
`agent_memory=`) after your `system_instruction`, and the turn block as a user part after the
contents. The answer is recorded as the agent's turn with Gemini's usage: prompt tokens, the
cached ones among them, and the model version. Outside a block, calls pass through untouched,
and nothing the wrapper does can fail your call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, TypeVar, cast

from niadra.conversation import _AsyncSession as AsyncSession
from niadra.conversation import _SyncSession as SyncSession
from niadra.integrations._common import (
    AgentMemoryLike,
    AgentMemoryOption,
    AnySession,
    Prompt,
    agent_turn,
    join_instructions,
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
_METHODS = ("generate_content", "generate_content_stream")


def wrap(client: C, *, conversation: AnySession | None = None, agent_memory: AgentMemoryLike = None) -> C:
    """A proxy of a `genai.Client` that injects the context and records the answers."""
    option = memory_option(agent_memory)
    models = client.models  # type: ignore[attr-defined]
    overrides: dict[str, Any] = {
        "models": _Proxy(models, {m: _sync(getattr(models, m), conversation, option, m) for m in _METHODS})
    }
    aio = getattr(client, "aio", None)
    if aio is not None:
        aio_models = aio.models
        wrapped = {m: _async(getattr(aio_models, m), conversation, option, m) for m in _METHODS}
        overrides["aio"] = _Proxy(aio, {"models": _Proxy(aio_models, wrapped)})
    return cast(C, _Proxy(client, overrides))


def _join_system(existing: Any, text: str) -> Any:
    if not existing:
        return text
    if isinstance(existing, str):
        return join_instructions(existing, text)
    if isinstance(existing, list):
        return [*existing, text]
    parts = existing.get("parts") if isinstance(existing, Mapping) else getattr(existing, "parts", None)
    if isinstance(existing, Mapping):
        return {**existing, "parts": [*(parts or []), {"text": text}]}
    if parts is not None and hasattr(existing, "model_copy"):
        from google.genai import types

        return existing.model_copy(update={"parts": [*parts, types.Part(text=text)]})
    return [existing, text]


def inject(prompt: Prompt, kwargs: dict[str, Any]) -> dict[str, Any]:
    """The request's keyword arguments with the prompt placed; the caller's objects are not changed."""
    result = dict(kwargs)
    if prompt.system:
        config = kwargs.get("config")
        if config is None:
            result["config"] = {"system_instruction": prompt.system}
        elif isinstance(config, Mapping):
            result["config"] = {
                **config,
                "system_instruction": _join_system(config.get("system_instruction"), prompt.system),
            }
        else:
            joined = _join_system(getattr(config, "system_instruction", None), prompt.system)
            result["config"] = config.model_copy(update={"system_instruction": joined})
    if prompt.turn:
        contents = kwargs.get("contents")
        if isinstance(contents, list) and contents and not isinstance(contents[-1], str):
            result["contents"] = [*contents, {"role": "user", "parts": [{"text": prompt.turn}]}]
        elif isinstance(contents, list):
            result["contents"] = [*contents, prompt.turn]
        elif contents is None or isinstance(contents, str):
            result["contents"] = [c for c in (contents, prompt.turn) if c]
        else:
            result["contents"] = [contents, {"role": "user", "parts": [{"text": prompt.turn}]}]
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


class _Answer:
    """The text and the usage of one call, from a response or from the chunks of a stream."""

    def __init__(self, session: AnySession, model: Any) -> None:
        self.session = session
        self.model = model
        self.parts: list[str] = []
        self.usage: Any = None
        self.version: Any = None
        self.done = False

    def observe(self, response: Any) -> None:
        try:
            text = response.text
        except Exception:
            text = None
        if isinstance(text, str):
            self.parts.append(text)
        self.usage = getattr(response, "usage_metadata", None) or self.usage
        self.version = getattr(response, "model_version", None) or self.version

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        try:
            usage = self.usage
            model = self.version if isinstance(self.version, str) else self.model
            reported = model_usage(
                "google",
                model if isinstance(model, str) else None,
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "cached_content_token_count", None) or 0,
            )
            agent_turn(self.session, "".join(self.parts), usage=reported)
        except Exception as exc:
            warn("record the model's answer", exc)


def _sync(original: Any, explicit: AnySession | None, option: AgentMemoryOption | None, name: str) -> Any:
    def call(*args: Any, **kwargs: Any) -> Any:
        session = _session(explicit, SyncSession)
        if session is None:
            return original(*args, **kwargs)
        try:
            prepared = run_sync(_prepared(session, option, kwargs))
        except TypeError as exc:
            warn("place the context", exc)
            prepared = kwargs
        result = original(*args, **prepared)
        answer = _Answer(session, kwargs.get("model"))
        if name == "generate_content_stream":
            return _chunks(result, answer)
        answer.observe(result)
        answer.finish()
        return result

    return call


def _chunks(stream: Any, answer: _Answer) -> Iterator[Any]:
    try:
        for chunk in stream:
            answer.observe(chunk)
            yield chunk
    finally:
        answer.finish()


def _async(original: Any, explicit: AnySession | None, option: AgentMemoryOption | None, name: str) -> Any:
    async def call(*args: Any, **kwargs: Any) -> Any:
        session = _session(explicit, AsyncSession)
        if session is None:
            return await original(*args, **kwargs)
        result = await original(*args, **await _prepared(session, option, kwargs))
        answer = _Answer(session, kwargs.get("model"))
        if name == "generate_content_stream":
            return _async_chunks(result, answer)
        answer.observe(result)
        answer.finish()
        return result

    return call


async def _async_chunks(stream: Any, answer: _Answer) -> AsyncIterator[Any]:
    try:
        async for chunk in stream:
            answer.observe(chunk)
            yield chunk
    finally:
        answer.finish()
