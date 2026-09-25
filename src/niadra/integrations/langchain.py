"""LangChain: the customer's memory in any chain or chat model, with `langchain-core` only.

```python
from langchain_openai import ChatOpenAI
from niadra import Niadra, phone
from niadra.integrations.langchain import NiadraCallbackHandler, context_runnable, history_tools

niadra = Niadra(channel="chat")
with niadra.conversation(thread_id, subject=phone(caller)) as conversation:
    model = ChatOpenAI(model="gpt-4.1").bind_tools(history_tools(conversation))
    chain = prompt | context_runnable(conversation) | model
    answer = chain.invoke({"question": text}, config={"callbacks": [NiadraCallbackHandler(conversation)]})
```

- **Context.** `context_runnable()` takes the prompt's messages (a list, or a `PromptValue`) and
  returns them with the pack as a `SystemMessage` right after the leading system messages and the
  turn block as a `SystemMessage` at the end; `with_context()` and `awith_context()` do the same
  for a list you build yourself.
- **Turns.** `NiadraCallbackHandler` records the customer's messages when a chat model starts,
  keyed by their position in the conversation (history replayed on the next call is not recorded
  twice), and the model's answer with its `usage_metadata` when the call ends.
- **Tools.** `history_tools()` are `StructuredTool`s with the kit's names, descriptions and JSON
  Schemas, bound to the customer; `agent_memory=` adds the agent memory tools.
- **Agent memory.** `agent_memory=True` (or `{"write": True, "max_tokens": 300, "tags": [...]}`) puts
  the agent's own notes right before the customer's context, in the same system message.

For LangGraph agents, see `niadra.integrations.langgraph`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

try:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
    from langchain_core.prompt_values import PromptValue
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import StructuredTool
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("LangChain is not installed: pip install 'niadra[langchain]'") from exc

from niadra.integrations._common import (
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

__all__ = [
    "NiadraCallbackHandler",
    "awith_context",
    "context_runnable",
    "history_tools",
    "inject",
    "with_context",
]


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as `StructuredTool`s bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [
        StructuredTool.from_function(
            func=_sync_call(kit, spec.name),
            coroutine=_async_call(kit, spec.name),
            name=spec.name,
            description=spec.description,
            args_schema=spec.parameters,
            infer_schema=False,
        )
        for spec in tool_specs(kit.definitions)
    ]


def _sync_call(kit: AnyKit, name: str) -> Any:
    def call(**arguments: Any) -> str:
        try:
            return run_sync(call_tool(kit, name, arguments))
        except TypeError as exc:
            warn(f"run {name} without await", exc)
            return '{"error": "use the async tool with an AsyncNiadra conversation"}'

    return call


def _async_call(kit: AnyKit, name: str) -> Any:
    async def call(**arguments: Any) -> str:
        return await call_tool(kit, name, arguments)

    return call


def inject(prompt: Prompt, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """`messages` with the prompt's system slot after the leading system messages and its turn
    block at the end. The messages you pass are not changed."""
    result = list(messages)
    if prompt.system:
        position = 0
        while position < len(result) and isinstance(result[position], SystemMessage):
            position += 1
        result.insert(position, SystemMessage(content=prompt.system))
    if prompt.turn:
        result.append(SystemMessage(content=prompt.turn))
    return result


def _messages(value: Any) -> list[BaseMessage]:
    if isinstance(value, PromptValue):
        return list(value.to_messages())
    return list(value)


async def awith_context(
    conversation: AnySession, messages: Any, agent_memory: AgentMemoryLike = None
) -> list[BaseMessage]:
    """The messages with the agent's notes, the pack and the turn block. Never raises."""
    return await _with(conversation, _messages(messages), memory_option(agent_memory))


def with_context(
    conversation: AnySession, messages: Any, agent_memory: AgentMemoryLike = None
) -> list[BaseMessage]:
    """`awith_context()` for a sync `Niadra` conversation."""
    listed = _messages(messages)
    try:
        return run_sync(_with(conversation, listed, memory_option(agent_memory)))
    except TypeError as exc:
        warn("place the context without await", exc)
        return listed


async def _with(
    conversation: AnySession, messages: list[BaseMessage], option: AgentMemoryOption | None
) -> list[BaseMessage]:
    prompt = await read_prompt(conversation, option)
    if prompt.context is not None and (prompt.system or prompt.turn):
        mark_injected(conversation, prompt.context)
    return inject(prompt, messages)


def context_runnable(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> Any:
    """A runnable for LCEL, between the prompt and the model: `prompt | context_runnable(c) | model`."""
    return RunnableLambda(
        lambda messages: with_context(conversation, messages, agent_memory),
        afunc=lambda messages: awith_context(conversation, messages, agent_memory),
        name="niadra_context",
    )


def customer_key(conversation: AnySession, position: int, text: str) -> str:
    """The idempotency key of the customer's `position`-th message, the same on every replay."""
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return f"{conversation.id}:customer:{position}:{digest}"


def record_customer(conversation: AnySession, messages: Sequence[BaseMessage]) -> None:
    """Records the human messages, keyed by their position among the conversation's human messages."""
    position = 0
    for message in messages:
        if isinstance(message, HumanMessage):
            position += 1
            said = message.text if isinstance(message.text, str) else str(message.content)
            if said:
                customer_turn(conversation, said, idempotency_key=customer_key(conversation, position, said))


def record_answer(conversation: AnySession, message: Any) -> None:
    """Records an `AIMessage` with text as the agent's turn, with the usage the provider reported."""
    if not isinstance(message, AIMessage):
        return
    said = message.text if isinstance(message.text, str) else ""
    if not said:
        return
    metadata = message.usage_metadata or {}
    details = metadata.get("input_token_details") or {}
    model = (message.response_metadata or {}).get("model_name") or (message.response_metadata or {}).get(
        "model"
    )
    usage = model_usage(
        None,
        model if isinstance(model, str) else None,
        metadata.get("input_tokens"),
        details.get("cache_read", 0),
        details.get("cache_creation", 0),
    )
    extra: dict[str, Any] = {}
    if message.id:
        extra["idempotency_key"] = f"{conversation.id}:agent:{message.id}"
    agent_turn(conversation, said, usage=usage, **extra)


class NiadraCallbackHandler(BaseCallbackHandler):  # type: ignore[misc]
    """Records the conversation from LangChain's callbacks: customer messages and model answers."""

    raise_error = False

    def __init__(self, conversation: AnySession) -> None:
        super().__init__()
        self.conversation = conversation

    def on_chat_model_start(self, serialized: Any, messages: list[list[BaseMessage]], **kwargs: Any) -> None:
        try:
            for batch in messages[:1]:
                record_customer(self.conversation, batch)
        except Exception as exc:
            warn("record the customer's turn", exc)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            generations = response.generations[0] if response.generations else []
            if generations:
                record_answer(self.conversation, getattr(generations[0], "message", None))
        except Exception as exc:
            warn("record the agent's turn", exc)
