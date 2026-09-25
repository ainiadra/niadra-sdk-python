"""LiteLLM: the customer's memory for any provider you route through LiteLLM.

```python
from niadra import Niadra, phone
from niadra.integrations.litellm import completion

niadra = Niadra(channel="chat")
with niadra.conversation(thread_id, subject=phone(caller)):
    response = completion(model="anthropic/claude-sonnet-4-5", messages=messages)
```

- `completion()` and `acompletion()` are `litellm.completion` and `litellm.acompletion` with the
  context: inside a conversation or task block, the pack (after the agent's own notes, with
  `agent_memory=`) goes after your leading system messages and the turn block at the end, and the
  answer (streamed or not) is recorded as the agent's turn with the usage LiteLLM normalized.
  Every other argument goes to LiteLLM unchanged; outside a block they are LiteLLM's own calls.
- `NiadraLogger` is a LiteLLM `CustomLogger` for code that calls LiteLLM (or its `Router`)
  directly: add it to `litellm.callbacks` and every successful call made inside a conversation
  block is recorded as the agent's turn, with its usage. It never injects: use `completion()`
  for that.
"""

from __future__ import annotations

from typing import Any

try:
    import litellm
    from litellm.integrations.custom_logger import CustomLogger
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("LiteLLM is not installed: pip install 'niadra[litellm]'") from exc

from niadra.conversation import AnySession, current_session
from niadra.conversation import _AsyncSession as AsyncSession
from niadra.conversation import _SyncSession as SyncSession
from niadra.integrations._common import (
    AgentMemoryLike,
    AgentMemoryOption,
    agent_turn,
    inject_prompt,
    mark_injected,
    memory_option,
    read_prompt,
    run_sync,
    warn,
)
from niadra.integrations.openai import _Answer, _answer, _session
from niadra.models.events import ModelUsage

__all__ = ["NiadraLogger", "acompletion", "completion"]

_RECORDED = "niadra_recorded"


def completion(
    *args: Any, conversation: AnySession | None = None, agent_memory: AgentMemoryLike = None, **kwargs: Any
) -> Any:
    """`litellm.completion` with the context and the answer recorded. See the module documentation."""
    session = _session(conversation, SyncSession)
    if session is None:
        return litellm.completion(*args, **kwargs)
    try:
        kwargs = run_sync(_prepared(session, memory_option(agent_memory), kwargs))
    except TypeError as exc:
        warn("place the context", exc)
    result = litellm.completion(*args, **_marked(kwargs))
    return _Answer(session, bool(kwargs.get("stream")), streams_async=False).take(result, raw=False)


async def acompletion(
    *args: Any, conversation: AnySession | None = None, agent_memory: AgentMemoryLike = None, **kwargs: Any
) -> Any:
    """`litellm.acompletion` with the context and the answer recorded."""
    session = _session(conversation, AsyncSession)
    if session is None:
        return await litellm.acompletion(*args, **kwargs)
    kwargs = await _prepared(session, memory_option(agent_memory), kwargs)
    result = await litellm.acompletion(*args, **_marked(kwargs))
    return _Answer(session, bool(kwargs.get("stream")), streams_async=True).take(result, raw=False)


def _marked(kwargs: dict[str, Any]) -> dict[str, Any]:
    # A `NiadraLogger` in `litellm.callbacks` must not record what `completion()` records itself.
    metadata = dict(kwargs.get("metadata") or {})
    metadata[_RECORDED] = True
    return {**kwargs, "metadata": metadata}


async def _prepared(
    session: AnySession, option: AgentMemoryOption | None, kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        prompt = await read_prompt(session, option)
        messages = kwargs.get("messages")
        if messages is None or not (prompt.system or prompt.turn):
            return kwargs
        if prompt.context is not None:
            mark_injected(session, prompt.context)
        return {**kwargs, "messages": inject_prompt(prompt, messages)}
    except Exception as exc:
        warn("place the context", exc)
        return kwargs


class NiadraLogger(CustomLogger):  # type: ignore[misc]
    """Records every successful LiteLLM call made inside a conversation block as the agent's turn.

    Pass `conversation=` to bind it to one conversation instead of the block that is running.
    """

    def __init__(self, conversation: AnySession | None = None) -> None:
        super().__init__()
        self.conversation = conversation

    def log_success_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self._record(kwargs, response_obj)

    async def async_log_success_event(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self._record(kwargs, response_obj)

    def _record(self, kwargs: Any, response: Any) -> None:
        try:
            metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
            if metadata.get(_RECORDED):
                return
            session = self.conversation or current_session()
            if session is None:
                return
            agent_turn(session, _answer(response), usage=ModelUsage.from_response(response))
        except Exception as exc:
            warn("record the model's answer", exc)
