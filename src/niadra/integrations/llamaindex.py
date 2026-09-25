"""LlamaIndex: the customer's memory as the agent's memory, and the history tools.

```python
from llama_index.core.agent.workflow import FunctionAgent
from niadra import AsyncNiadra, phone
from niadra.integrations.llamaindex import NiadraMemory, history_tools

niadra = AsyncNiadra(channel="chat")
conversation = niadra.conversation(thread_id, subject=phone(caller))
agent = FunctionAgent(llm=llm, system_prompt="You are Acme's agent.", tools=history_tools(conversation))
response = await agent.run(text, memory=NiadraMemory(conversation))
```

- **Context.** `NiadraMemory` wraps the chat memory your agent keeps (a `ChatMemoryBuffer` by
  default): `get()` returns the pack (after the agent's own notes, with `agent_memory=`) as a
  system message first, which the agent places right after its system prompt, then your chat
  history, then the turn block. Only the history is stored; the Niadra messages are made fresh on
  every read.
- **Turns.** Every message the agent puts into memory is recorded: the user's as the customer's
  turn and the assistant's text as the agent's.
- **Tools.** `history_tools()` gives the history tools as LlamaIndex tools whose parameters are
  the kit's JSON Schemas, bound to the customer.

The shape (a primary chat memory wrapped, a system message placed on `get()`, writes passed on
in `put()`) follows `Mem0Memory` of LlamaIndex's Mem0 integration (`llama-index-memory-mem0`,
MIT License); no code is copied. The reading is Niadra's: one pinned pack per conversation
instead of a search per message.
"""

from __future__ import annotations

from typing import Any

try:
    from llama_index.core.base.llms.types import ChatMessage, MessageRole
    from llama_index.core.memory import BaseMemory, ChatMemoryBuffer
    from llama_index.core.tools import ToolMetadata, ToolOutput
    from llama_index.core.tools.types import AsyncBaseTool
    from pydantic import Field, PrivateAttr
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("LlamaIndex is not installed: pip install 'niadra[llamaindex]'") from exc

from niadra.integrations._common import (
    MARK,
    AgentMemoryLike,
    AgentMemoryOption,
    AnyKit,
    AnySession,
    Prompt,
    ToolSpec,
    agent_turn,
    call_tool,
    customer_turn,
    mark_injected,
    memory_kit_of,
    memory_option,
    read_prompt,
    run_sync,
    tool_specs,
    warn,
)

__all__ = ["NiadraMemory", "NiadraTool", "history_tools"]


class _Metadata(ToolMetadata):  # type: ignore[misc]
    """Tool metadata whose parameters are a JSON Schema as it is, not built from a model."""

    def __init__(self, spec: ToolSpec) -> None:
        super().__init__(description=spec.description, name=spec.name, fn_schema=None)
        self.parameters = spec.parameters

    def get_parameters_dict(self) -> dict[str, Any]:
        return dict(self.parameters)


class NiadraTool(AsyncBaseTool):  # type: ignore[misc]
    """One history tool as a LlamaIndex tool, bound to the customer."""

    def __init__(self, kit: AnyKit, spec: ToolSpec) -> None:
        self._kit = kit
        self._spec = spec
        self._metadata = _Metadata(spec)

    @property
    def metadata(self) -> Any:
        return self._metadata

    def _output(self, arguments: dict[str, Any], text: str) -> Any:
        return ToolOutput(content=text, tool_name=self._spec.name, raw_input=arguments, raw_output=text)

    def call(self, *args: Any, **kwargs: Any) -> Any:
        try:
            text = run_sync(call_tool(self._kit, self._spec.name, kwargs))
        except TypeError as exc:
            warn(f"run {self._spec.name} without await", exc)
            text = '{"error": "use acall with an AsyncNiadra conversation"}'
        return self._output(kwargs, text)

    async def acall(self, *args: Any, **kwargs: Any) -> Any:
        return self._output(kwargs, await call_tool(self._kit, self._spec.name, kwargs))


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as LlamaIndex tools bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [NiadraTool(kit, spec) for spec in tool_specs(kit.definitions)]


def _niadra(text: str) -> Any:
    return ChatMessage(role=MessageRole.SYSTEM, content=text, additional_kwargs={MARK: True})


def _ours(message: Any) -> bool:
    return bool((getattr(message, "additional_kwargs", None) or {}).get(MARK))


class NiadraMemory(BaseMemory):  # type: ignore[misc]
    """A LlamaIndex memory: your chat history with the customer's context around it."""

    conversation: Any = Field(exclude=True)
    history: Any = Field(default=None, exclude=True)
    agent_memory: Any = Field(default=None, exclude=True)
    _option: AgentMemoryOption | None = PrivateAttr(default=None)

    def __init__(
        self,
        conversation: AnySession,
        *,
        history: Any = None,
        agent_memory: AgentMemoryLike = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            conversation=conversation, history=history or ChatMemoryBuffer.from_defaults(), **kwargs
        )
        self._option = memory_option(agent_memory)

    @classmethod
    def class_name(cls) -> str:
        return "NiadraMemory"

    @classmethod
    def from_defaults(cls, **kwargs: Any) -> NiadraMemory:
        return cls(**kwargs)

    def _around(self, prompt: Prompt, messages: list[Any]) -> list[Any]:
        if prompt.context is not None and (prompt.system or prompt.turn):
            mark_injected(self.conversation, prompt.context)
        head = [_niadra(prompt.system)] if prompt.system else []
        tail = [_niadra(prompt.turn)] if prompt.turn else []
        return [*head, *messages, *tail]

    def get(self, input: str | None = None, **kwargs: Any) -> list[Any]:
        messages = list(self.history.get(input=input, **kwargs))
        try:
            return self._around(run_sync(read_prompt(self.conversation, self._option)), messages)
        except TypeError as exc:
            warn("read the context without await", exc)
            return messages

    async def aget(self, input: str | None = None, **kwargs: Any) -> list[Any]:
        messages = list(await self.history.aget(input=input, **kwargs))
        return self._around(await read_prompt(self.conversation, self._option), messages)

    def get_all(self) -> list[Any]:
        return list(self.history.get_all())

    def _record(self, message: Any) -> None:
        content = message.content if isinstance(message.content, str) else None
        if message.role == MessageRole.USER:
            customer_turn(self.conversation, content)
        elif message.role == MessageRole.ASSISTANT:
            agent_turn(self.conversation, content)

    def put(self, message: Any) -> None:
        if _ours(message):
            return
        self.history.put(message)
        self._record(message)

    async def aput(self, message: Any) -> None:
        if _ours(message):
            return
        await self.history.aput(message)
        self._record(message)

    def set(self, messages: list[Any]) -> None:
        self.history.set([m for m in messages if not _ours(m)])

    def reset(self) -> None:
        self.history.reset()
