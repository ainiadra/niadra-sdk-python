"""Microsoft Agent Framework: the customer's memory as a context provider.

```python
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from niadra import AsyncNiadra, phone
from niadra.integrations.agent_framework import NiadraContextProvider

niadra = AsyncNiadra(channel="chat")
conversation = niadra.conversation(thread_id, subject=phone(caller))
agent = Agent(
    client=OpenAIChatClient(),
    instructions="You are Acme's agent.",
    context_providers=[NiadraContextProvider(conversation)],
)
response = await agent.run(text)
```

- **Context.** Before each run the provider adds the pack (after the agent's own notes, with
  `agent_memory=`) and the turn block as instructions, which the framework places after the
  agent's own, and adds the history tools.
- **Turns.** The run's new user messages are the customer's turns; the run's response is the
  agent's turn, with its usage (input tokens, cache reads and writes) and model.
- **Tools.** The history tools are `FunctionTool`s whose input model is the kit's JSON Schema,
  bound to the customer.
- **Handoff.** `transferred_to_agent()` and `transferred_to_human()` record a transfer, for a
  workflow that hands over or an escalation to a person.

This is the adapter for the Agent Framework, AutoGen's successor; an AutoGen 0.4 agent can use
the kit's tools and `conversation.context()` directly.
"""

from __future__ import annotations

from typing import Any

try:
    from agent_framework import ContextProvider, FunctionTool
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("The Agent Framework is not installed: pip install 'niadra[agent-framework]'") from exc

from niadra.integrations._common import (
    AgentMemoryLike,
    AnyKit,
    AnySession,
    agent_turn,
    call_tool,
    customer_turn,
    handoff,
    join_instructions,
    mark_injected,
    memory_kit_of,
    memory_option,
    model_usage,
    read_prompt,
    tool_specs,
    warn,
)

__all__ = ["NiadraContextProvider", "history_tools"]

SOURCE_ID = "niadra"


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as Agent Framework `FunctionTool`s bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [
        FunctionTool(
            name=spec.name,
            description=spec.description,
            func=_run(kit, spec.name),
            input_model=spec.parameters,
        )
        for spec in tool_specs(kit.definitions)
    ]


def _run(kit: AnyKit, name: str) -> Any:
    async def run(**arguments: Any) -> str:
        return await call_tool(kit, name, arguments)

    return run


def _text(message: Any) -> str:
    text = getattr(message, "text", None)
    return text if isinstance(text, str) else ""


class NiadraContextProvider(ContextProvider):  # type: ignore[misc]
    """A context provider that reads and records one Niadra conversation."""

    def __init__(
        self,
        conversation: AnySession,
        *,
        history_tools: bool = True,
        agent_memory: AgentMemoryLike = None,
        source_id: str = SOURCE_ID,
    ) -> None:
        super().__init__(source_id)
        self.conversation = conversation
        self.agent_memory = memory_option(agent_memory)
        self.tools: list[Any] = _tools(conversation, agent_memory) if history_tools else []

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    async def before_run(self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]) -> None:
        try:
            for message in context.input_messages or []:
                if str(getattr(message, "role", "")) == "user":
                    customer_turn(self.conversation, _text(message))
            if self.tools:
                context.extend_tools(self.source_id, self.tools)
            prompt = await read_prompt(self.conversation, self.agent_memory)
            text = join_instructions(prompt.system, prompt.turn)
            if text:
                context.extend_instructions(self.source_id, text)
                if prompt.context is not None:
                    mark_injected(self.conversation, prompt.context)
        except Exception as exc:
            warn("place the context", exc)

    async def after_run(self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]) -> None:
        try:
            response = context.response
            if response is None:
                return
            usage = getattr(response, "usage_details", None) or {}
            read = usage.get("cache_read_input_token_count") or 0
            written = usage.get("cache_creation_input_token_count") or 0
            # The agent's response keeps the chat response it came from, which names the model.
            raw = getattr(response, "raw_representation", None)
            model = getattr(response, "model", None) or getattr(raw, "model", None)
            reported = model_usage(
                None,
                model if isinstance(model, str) else None,
                usage.get("input_token_count"),
                read,
                written,
            )
            agent_turn(self.conversation, _text(response), usage=reported)
        except Exception as exc:
            warn("record the agent's turn", exc)


def _tools(conversation: AnySession, agent_memory: AgentMemoryLike) -> list[Any]:
    try:
        return history_tools(conversation, agent_memory)
    except Exception as exc:
        warn("build the history tools", exc)
        return []
