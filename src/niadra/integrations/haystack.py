"""Haystack: the customer's memory in a pipeline or an agent, as components, hooks and tools.

In a pipeline, `NiadraContext` goes between the prompt builder and the chat generator, and
`NiadraReply` after the generator:

```python
from haystack import Pipeline
from haystack.components.builders import ChatPromptBuilder
from haystack.components.generators.chat import OpenAIChatGenerator
from niadra import Niadra, phone
from niadra.integrations.haystack import NiadraContext, NiadraReply

niadra = Niadra(channel="chat")
conversation = niadra.conversation(thread_id, subject=phone(caller))
pipeline = Pipeline()
pipeline.add_component("prompt", ChatPromptBuilder())
pipeline.add_component("niadra", NiadraContext(conversation))
pipeline.add_component("llm", OpenAIChatGenerator())
pipeline.add_component("reply", NiadraReply(conversation))
pipeline.connect("prompt.prompt", "niadra.messages")
pipeline.connect("niadra.messages", "llm.messages")
pipeline.connect("llm.replies", "reply.replies")
```

With an `Agent`, the hooks and the tools do the same:

```python
from haystack.components.agents import Agent
from niadra.integrations.haystack import NiadraAgentHooks, history_tools

memory = NiadraAgentHooks(conversation)
agent = Agent(
    chat_generator=OpenAIChatGenerator(),
    system_prompt="You are Acme's agent.",
    tools=history_tools(conversation),
    hooks=memory.hooks,
)
result = agent.run(messages=[ChatMessage.from_user(text)])
```

- **Context.** The pack (after the agent's own notes, with `agent_memory=`) goes right after the
  leading system messages and the turn block at the end, as system messages. Messages Niadra put
  in an earlier turn are taken out first, so a history passed back in never holds them twice, and
  the agent's `messages` output never holds them.
- **Turns.** `NiadraContext` and the `before_run` hook record the last user message as the
  customer's turn; `NiadraReply` and the `after_run` hook record the assistant's text as the
  agent's, with its usage and model.
- **Tools.** `history_tools()` gives Haystack `Tool`s whose parameters are the kit's JSON Schemas,
  bound to the customer, with a sync and an async function.

The components and hooks take `Niadra` and `AsyncNiadra` conversations; with `AsyncNiadra`, run
the pipeline or the agent with `run_async`. Niadra slow or down never stops the pipeline: the
messages go on without the context and a tool answers that the history is unavailable.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

try:
    from haystack import component
    from haystack.components.agents.state import replace_values
    from haystack.core.serialization import generate_qualified_class_name
    from haystack.dataclasses import ChatMessage, ChatRole
    from haystack.hooks import AFTER_RUN, BEFORE_LLM, BEFORE_RUN
    from haystack.tools import Tool
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("Haystack is not installed: pip install 'niadra[haystack]'") from exc

from niadra.integrations._common import (
    MARK,
    AgentMemoryLike,
    AgentMemoryOption,
    AnyKit,
    AnySession,
    Prompt,
    agent_turn,
    call_tool,
    customer_turn,
    mark_injected,
    memory_kit_of,
    memory_option,
    model_usage,
    read_prompt,
    run_sync,
    tool_specs,
    warn,
)

__all__ = ["NiadraAgentHooks", "NiadraContext", "NiadraReply", "history_tools"]

_UNAVAILABLE_SYNC = '{"error": "use run_async with an AsyncNiadra conversation"}'


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as Haystack `Tool`s bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [
        Tool(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            function=_sync(kit, spec.name),
            async_function=_async(kit, spec.name),
        )
        for spec in tool_specs(kit.definitions)
    ]


def _sync(kit: AnyKit, name: str) -> Callable[..., str]:
    def run(**arguments: Any) -> str:
        try:
            return run_sync(call_tool(kit, name, arguments))
        except TypeError as exc:
            warn(f"run {name} without await", exc)
            return _UNAVAILABLE_SYNC

    return run


def _async(kit: AnyKit, name: str) -> Callable[..., Coroutine[Any, Any, str]]:
    async def run(**arguments: Any) -> str:
        return await call_tool(kit, name, arguments)

    return run


def _niadra(text: str) -> Any:
    return ChatMessage.from_system(text, meta={MARK: True})


def _ours(message: Any) -> bool:
    return bool((getattr(message, "meta", None) or {}).get(MARK))


def _place(prompt: Prompt, messages: list[Any]) -> list[Any]:
    """The messages without earlier Niadra messages, with this turn's placed around them."""
    result = [m for m in messages if not _ours(m)]
    if prompt.system:
        count = 0
        while count < len(result) and result[count].is_from(ChatRole.SYSTEM):
            count += 1
        result.insert(count, _niadra(prompt.system))
    if prompt.turn:
        result.append(_niadra(prompt.turn))
    return result


def _last_user_text(messages: list[Any]) -> str | None:
    for message in reversed(messages):
        if message.is_from(ChatRole.USER) and not _ours(message):
            return message.text  # type: ignore[no-any-return]
    return None


def _record_reply(conversation: AnySession, message: Any) -> None:
    """Records an assistant message's text, with the usage its generator put in `meta`."""
    if not message.is_from(ChatRole.ASSISTANT) or message.tool_calls:
        return
    meta = message.meta or {}
    usage = meta.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    cached = details.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    reported = model_usage(None, meta.get("model"), prompt_tokens, cached, written)
    agent_turn(conversation, message.text, usage=reported)


class _Placer:
    """Reads the prompt for the conversation and stamps it; empty on any failure."""

    def __init__(self, conversation: AnySession, agent_memory: AgentMemoryLike) -> None:
        self.conversation = conversation
        self.agent_memory: AgentMemoryOption | None = memory_option(agent_memory)

    async def prompt(self) -> Prompt:
        try:
            prompt = await read_prompt(self.conversation, self.agent_memory)
        except Exception as exc:
            warn("read the context", exc)
            return Prompt()
        if prompt.context is not None and (prompt.system or prompt.turn):
            mark_injected(self.conversation, prompt.context)
        return prompt

    def prompt_sync(self) -> Prompt:
        try:
            return run_sync(self.prompt())
        except TypeError as exc:
            warn("read the context without await", exc)
            return Prompt()


@component
class NiadraContext:
    """A pipeline component: records the customer's message and places the context in the messages."""

    def __init__(self, conversation: AnySession, *, agent_memory: AgentMemoryLike = None) -> None:
        self.conversation = conversation
        self._placer = _Placer(conversation, agent_memory)

    @component.output_types(messages=list[ChatMessage])
    def run(self, messages: list[ChatMessage]) -> dict[str, Any]:
        customer_turn(self.conversation, _last_user_text(messages))
        return {"messages": self._placed(self._placer.prompt_sync(), messages)}

    @component.output_types(messages=list[ChatMessage])
    async def run_async(self, messages: list[ChatMessage]) -> dict[str, Any]:
        customer_turn(self.conversation, _last_user_text(messages))
        return {"messages": self._placed(await self._placer.prompt(), messages)}

    def _placed(self, prompt: Prompt, messages: list[Any]) -> list[Any]:
        try:
            return _place(prompt, list(messages))
        except Exception as exc:
            warn("place the context", exc)
            return list(messages)


@component
class NiadraReply:
    """A pipeline component: records the generator's replies as the agent's turn and passes them on."""

    def __init__(self, conversation: AnySession) -> None:
        self.conversation = conversation

    @component.output_types(replies=list[ChatMessage])
    def run(self, replies: list[ChatMessage]) -> dict[str, Any]:
        for reply in replies:
            try:
                _record_reply(self.conversation, reply)
            except Exception as exc:
                warn("record the agent's turn", exc)
        return {"replies": replies}

    @component.output_types(replies=list[ChatMessage])
    async def run_async(self, replies: list[ChatMessage]) -> dict[str, Any]:
        passed: dict[str, Any] = self.run(replies)
        return passed


class _Hook:
    """One agent hook: a sync and an async function of the agent's live `State`."""

    def __init__(self, sync: Callable[[Any], None], asynchronous: Callable[[Any], Coroutine[Any, Any, None]]):
        self._sync = sync
        self._async = asynchronous

    def run(self, state: Any) -> None:
        self._sync(state)

    async def run_async(self, state: Any) -> None:
        await self._async(state)

    def to_dict(self) -> dict[str, Any]:
        # A hook bound to a live conversation is not serialized; rebuild it where the agent loads.
        return {"type": generate_qualified_class_name(type(self)), "init_parameters": {}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _Hook:
        raise NotImplementedError("rebuild NiadraAgentHooks for the conversation instead")


class NiadraAgentHooks:
    """Hooks that wire one Niadra conversation into a Haystack `Agent`: pass `hooks` to the agent."""

    def __init__(self, conversation: AnySession, *, agent_memory: AgentMemoryLike = None) -> None:
        self.conversation = conversation
        self._placer = _Placer(conversation, agent_memory)
        self.hooks: dict[str, list[Any]] = {
            BEFORE_RUN: [_Hook(self._before_run, self._before_run_async)],
            BEFORE_LLM: [_Hook(self._before_llm, self._before_llm_async)],
            AFTER_RUN: [_Hook(self._after_run, self._after_run_async)],
        }

    def _before_run(self, state: Any) -> None:
        try:
            customer_turn(self.conversation, _last_user_text(state.data.get("messages") or []))
        except Exception as exc:
            warn("record the customer's turn", exc)

    async def _before_run_async(self, state: Any) -> None:
        self._before_run(state)

    def _set(self, state: Any, prompt: Prompt) -> None:
        try:
            placed = _place(prompt, list(state.data.get("messages") or []))
            state.set("messages", placed, handler_override=replace_values)
        except Exception as exc:
            warn("place the context", exc)

    def _before_llm(self, state: Any) -> None:
        self._set(state, self._placer.prompt_sync())

    async def _before_llm_async(self, state: Any) -> None:
        self._set(state, await self._placer.prompt())

    def _after_run(self, state: Any) -> None:
        try:
            self._set(state, Prompt())
            messages = state.data.get("messages") or []
            if messages:
                _record_reply(self.conversation, messages[-1])
        except Exception as exc:
            warn("record the agent's turn", exc)

    async def _after_run_async(self, state: Any) -> None:
        self._after_run(state)
