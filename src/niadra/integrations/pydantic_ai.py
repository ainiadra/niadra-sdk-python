"""Pydantic AI: the customer's memory as one agent capability.

```python
from pydantic_ai import Agent
from niadra import AsyncNiadra, phone
from niadra.integrations.pydantic_ai import NiadraCapability

niadra = AsyncNiadra(channel="chat")
conversation = niadra.conversation(thread_id, subject=phone(caller))
capability = NiadraCapability(conversation)
agent = Agent("openai:gpt-4.1", instructions="You are Acme's agent.", capabilities=[capability])
result = await agent.run(text)
```

- **Context.** The capability's instructions are the pack (after the agent's own notes, with
  `agent_memory=`) and then the turn block, rendered for every model request after the agent's
  own instructions; instructions are not stored in the message history, so nothing piles up.
- **Turns.** The run's prompt is recorded as the customer's turn when the run starts (keyed by the
  run id) and each model answer with its usage (input tokens, cache reads and writes) and model
  name when the request returns.
- **Tools.** The capability's toolset holds the history tools, built from the kit's JSON Schemas
  with `Tool.from_schema`, bound to the customer.
- **Handoff.** `transferred_to_agent()` and `transferred_to_human()` record a transfer, for agent
  delegation or a hand-off to a person.
"""

from __future__ import annotations

from typing import Any

try:
    from pydantic_ai import Tool
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.toolsets import FunctionToolset
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("Pydantic AI is not installed: pip install 'niadra[pydantic-ai]'") from exc

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

__all__ = ["NiadraCapability", "history_tools"]


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as Pydantic AI tools bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [
        Tool.from_schema(_run(kit, spec.name), spec.name, spec.description, spec.parameters)
        for spec in tool_specs(kit.definitions)
    ]


def _run(kit: AnyKit, name: str) -> Any:
    async def run(**arguments: Any) -> str:
        return await call_tool(kit, name, arguments)

    return run


def _prompt_text(prompt: Any) -> str:
    if isinstance(prompt, str):
        return prompt
    return "\n".join(part for part in prompt or [] if isinstance(part, str))


class NiadraCapability(AbstractCapability[Any]):  # type: ignore[misc]
    """Instructions, tools and hooks that wire one Niadra conversation into a Pydantic AI agent."""

    def __init__(
        self, conversation: AnySession, *, history_tools: bool = True, agent_memory: AgentMemoryLike = None
    ) -> None:
        super().__init__()
        self.conversation = conversation
        self.agent_memory = memory_option(agent_memory)
        self._tools = _tools(conversation, agent_memory) if history_tools else []

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    def get_instructions(self) -> Any:
        return self._instructions

    def get_toolset(self) -> Any:
        return FunctionToolset(self._tools) if self._tools else None

    async def _instructions(self, ctx: Any) -> str:
        try:
            prompt = await read_prompt(self.conversation, self.agent_memory)
            text = join_instructions(prompt.system, prompt.turn)
            if text and prompt.context is not None:
                mark_injected(self.conversation, prompt.context)
            return text
        except Exception as exc:
            warn("place the context", exc)
            return ""

    async def before_run(self, ctx: Any) -> None:
        said = _prompt_text(getattr(ctx, "prompt", None))
        run = getattr(ctx, "run_id", None)
        extra = {"idempotency_key": f"{self.conversation.id}:customer:{run}"} if run else {}
        customer_turn(self.conversation, said, **extra)

    async def after_model_request(self, ctx: Any, *, request_context: Any, response: Any) -> Any:
        try:
            said = "".join(
                part.content
                for part in response.parts
                if getattr(part, "part_kind", "") == "text" and isinstance(part.content, str)
            )
            usage = response.usage
            read = getattr(usage, "cache_read_tokens", 0) or 0
            written = getattr(usage, "cache_write_tokens", 0) or 0
            reported = model_usage(
                getattr(response, "provider_name", None),
                getattr(response, "model_name", None),
                getattr(usage, "input_tokens", None),
                read,
                written,
            )
            agent_turn(self.conversation, said, usage=reported)
        except Exception as exc:
            warn("record the agent's turn", exc)
        return response


def _tools(conversation: AnySession, agent_memory: AgentMemoryLike) -> list[Any]:
    try:
        return history_tools(conversation, agent_memory)
    except Exception as exc:
        warn("build the history tools", exc)
        return []
