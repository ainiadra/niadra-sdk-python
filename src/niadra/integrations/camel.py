"""CAMEL-AI: the customer's memory as a CAMEL agent memory, and the history tools as a toolkit.

```python
from camel.agents import ChatAgent
from niadra import Niadra, phone
from niadra.integrations.camel import NiadraMemory

niadra = Niadra(channel="chat")
conversation = niadra.conversation(thread_id, subject=phone(caller))
memory = NiadraMemory(conversation)
agent = memory.attach(ChatAgent(system_message="You are Acme's agent.", model=model, tools=memory.tools))
response = agent.step(text)
```

- **Context.** `NiadraMemory` is an `AgentMemory` around the chat memory the agent keeps (the
  agent's own `ChatHistoryMemory`, with its context creator, once attached): `get_context()`
  returns the pack (after the agent's own notes, with `agent_memory=`) right after the system
  message, then the chat history, then the turn block. Only the history is stored; the Niadra
  messages are made fresh on every read.
- **Turns.** Every record the agent writes is recorded: the user's message as the customer's
  turn and the assistant's text as the agent's; tool calls and results are not turns.
- **Tools.** `memory.tools` (and `NiadraToolkit(conversation).get_tools()`) are CAMEL
  `FunctionTool`s whose OpenAI schema is the kit's definition, word for word, bound to the
  customer.
- **Handoff.** `transferred_to_agent()` and `transferred_to_human()` record a transfer, for a
  workforce that hands the conversation over or an escalation to a person.

CAMEL reads an agent's memory synchronously, even in `astep`: the context comes with a `Niadra`
conversation (an `AsyncNiadra` one gives the tools and the turns, and the agent runs without the
context). The read has the SDK's own time budget, 300 ms for chat and 150 ms for voice. Niadra
slow or down never stops the agent: it runs without the context and a tool answers that the
history is unavailable.
"""

from __future__ import annotations

import warnings
from typing import Any

try:
    from camel.memories import AgentMemory, ChatHistoryMemory
    from camel.messages import FunctionCallingMessage
    from camel.toolkits import BaseToolkit, FunctionTool
    from camel.types import OpenAIBackendRole
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("CAMEL-AI is not installed: pip install 'niadra[camel]'") from exc

from niadra.integrations._common import (
    AgentMemoryLike,
    AnyKit,
    AnySession,
    agent_turn,
    call_tool,
    call_tool_sync,
    customer_turn,
    handoff,
    inject_prompt,
    mark_injected,
    memory_kit_of,
    memory_option,
    read_prompt,
    run_sync,
    tool_specs,
    warn,
)
from niadra.tools import AsyncToolKit, ToolKit

__all__ = ["NiadraMemory", "NiadraTool", "NiadraToolkit", "history_tools"]


def _function(kit: AnyKit, name: str) -> Any:
    if isinstance(kit, AsyncToolKit):

        async def arun(**arguments: Any) -> str:
            return await call_tool(kit, name, arguments)

        arun.__name__ = name
        return arun

    sync_kit: ToolKit = kit

    def run(**arguments: Any) -> str:
        return call_tool_sync(sync_kit, name, arguments)

    run.__name__ = name
    return run


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as CAMEL `FunctionTool`s bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [
        NiadraTool(
            _function(kit, spec.name),
            openai_tool_schema={
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            },
        )
        for spec in tool_specs(kit.definitions)
    ]


class NiadraTool(FunctionTool):  # type: ignore[misc]
    """A history tool: a `FunctionTool` whose schema is the kit's, checked without the noise."""

    @staticmethod
    def validate_openai_tool_schema(openai_tool_schema: dict[str, Any]) -> None:
        with warnings.catch_warnings():
            # The kit leaves self-explanatory fields (cursor, limit) without a description, on
            # purpose; CAMEL would warn about each on every read of the schema.
            warnings.filterwarnings("ignore", message="Parameter description is missing")
            FunctionTool.validate_openai_tool_schema(openai_tool_schema)


class NiadraToolkit(BaseToolkit):  # type: ignore[misc]
    """The history tools as a CAMEL toolkit, for a workforce or a role-playing session."""

    def __init__(
        self, conversation: AnySession, *, agent_memory: AgentMemoryLike = None, timeout: float | None = None
    ) -> None:
        super().__init__(timeout=timeout)
        self.conversation = conversation
        self.agent_memory = agent_memory

    def get_tools(self) -> list[Any]:
        return _tools(self.conversation, self.agent_memory)


def _said(record: Any) -> str | None:
    message = getattr(record, "message", None)
    if message is None or isinstance(message, FunctionCallingMessage):
        return None
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else None


class NiadraMemory(AgentMemory):  # type: ignore[misc]
    """A CAMEL agent memory: your chat history with the customer's context around it."""

    def __init__(
        self,
        conversation: AnySession,
        memory: Any = None,
        *,
        history_tools: bool = True,
        agent_memory: AgentMemoryLike = None,
    ) -> None:
        self.conversation = conversation
        self.memory = memory
        self._option = memory_option(agent_memory)
        self.tools: list[Any] = _tools(conversation, agent_memory) if history_tools else []

    def attach(self, agent: Any) -> Any:
        """Makes this the agent's memory, around a chat memory with the agent's context creator."""
        if self.memory is None:
            self.memory = ChatHistoryMemory(agent.memory.get_context_creator(), agent_id=agent.agent_id)
        agent.memory = self  # the agent writes its system message into it
        return agent

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    def _inner(self) -> Any:
        if self.memory is None:
            raise RuntimeError("NiadraMemory has no chat memory: pass one, or call attach(agent)")
        return self.memory

    @property
    def agent_id(self) -> str | None:
        return self._inner().agent_id  # type: ignore[no-any-return]

    @agent_id.setter
    def agent_id(self, value: str | None) -> None:
        self._inner().agent_id = value

    def retrieve(self) -> list[Any]:
        return list(self._inner().retrieve())

    def get_context_creator(self) -> Any:
        return self._inner().get_context_creator()

    def get_context(self) -> tuple[list[Any], int]:
        messages, tokens = self._inner().get_context()
        try:
            prompt = run_sync(read_prompt(self.conversation, self._option))
        except TypeError as exc:
            warn("read the context without await", exc)
            return messages, tokens
        except Exception as exc:
            warn("read the context", exc)
            return messages, tokens
        if prompt.context is not None and (prompt.system or prompt.turn):
            mark_injected(self.conversation, prompt.context)
        return inject_prompt(prompt, messages), tokens

    def write_records(self, records: list[Any]) -> None:
        self._inner().write_records(records)
        for record in records:
            try:
                role = record.role_at_backend
                if role == OpenAIBackendRole.USER:
                    customer_turn(self.conversation, _said(record))
                elif role == OpenAIBackendRole.ASSISTANT:
                    agent_turn(self.conversation, _said(record))
            except Exception as exc:
                warn("record a turn", exc)

    def clear(self) -> None:
        self._inner().clear()

    def pop_records(self, count: int) -> list[Any]:
        return list(self._inner().pop_records(count))

    def remove_records_by_indices(self, indices: list[int]) -> list[Any]:
        return list(self._inner().remove_records_by_indices(indices))

    def clean_tool_calls(self) -> None:
        self._inner().clean_tool_calls()

    def __getattr__(self, name: str) -> Any:
        # What CAMEL reads from its own memories (the chat history block) comes from the wrapped one.
        memory = self.__dict__.get("memory")
        if memory is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(memory, name)


def _tools(conversation: AnySession, agent_memory: AgentMemoryLike) -> list[Any]:
    try:
        return history_tools(conversation, agent_memory)
    except Exception as exc:
        warn("build the history tools", exc)
        return []
