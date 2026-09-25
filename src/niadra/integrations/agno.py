"""Agno: the customer's memory in an Agno agent, as instructions, tools and run hooks.

```python
from agno.agent import Agent
from niadra import AsyncNiadra, phone
from niadra.integrations.agno import NiadraAgno

niadra = AsyncNiadra(channel="chat")
conversation = niadra.conversation(session_id, subject=phone(caller))
memory = NiadraAgno(conversation, instructions="You are Acme's agent.")
agent = Agent(
    model=model,
    instructions=memory.instructions,
    tools=memory.tools,
    pre_hooks=[memory.pre_hook],
    post_hooks=[memory.post_hook],
)
await agent.arun(text)
```

- **Context.** `instructions` is a dynamic instruction: your own instructions first, then the
  agent's own notes (with `agent_memory=`), the pack and the turn block, read for every run. With
  an `AsyncNiadra` conversation it is a coroutine function, so use `arun`.
- **Turns.** `pre_hook` records the run's input as the customer's turn and `post_hook` the run's
  answer as the agent's, with the run's usage and model.
- **Tools.** `tools` are Agno `Function`s with the kit's names, descriptions and JSON Schemas,
  bound to the customer.
- **Handoff.** `transferred_to_agent()` and `transferred_to_human()` record a transfer, for a team
  that delegates or a hand-off to a person.

Agno's own memory and storage stay Agno's; Niadra is the company's memory of the customer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

try:
    from agno.tools.function import Function
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("Agno is not installed: pip install 'niadra[agno]'") from exc

from niadra.conversation import AsyncConversation, AsyncTask
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
    run_sync,
    tool_specs,
    warn,
)

__all__ = ["NiadraAgno", "history_tools"]


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as Agno `Function`s bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [
        Function(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            entrypoint=_run(kit, spec.name),
            skip_entrypoint_processing=True,
        )
        for spec in tool_specs(kit.definitions)
    ]


def _run(kit: AnyKit, name: str) -> Any:
    async def run(**arguments: Any) -> str:
        return await call_tool(kit, name, arguments)

    return run


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    return content if isinstance(content, str) else None


class NiadraAgno:
    """Instructions, tools and hooks that wire one Niadra conversation into an Agno agent."""

    def __init__(
        self,
        conversation: AnySession,
        *,
        instructions: str | Sequence[str] | None = None,
        history_tools: bool = True,
        agent_memory: AgentMemoryLike = None,
    ) -> None:
        self.conversation = conversation
        own = [instructions] if isinstance(instructions, str) else list(instructions or [])
        self.own_instructions: list[str] = own
        self.agent_memory = memory_option(agent_memory)
        self.tools: list[Any] = _tools(conversation, agent_memory) if history_tools else []
        is_async = isinstance(conversation, (AsyncConversation, AsyncTask))
        self.instructions: Any = self._ainstructions if is_async else self._instructions

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    def _with(self, text: str) -> list[str]:
        return [*self.own_instructions, text] if text else list(self.own_instructions)

    async def _ainstructions(self) -> list[str]:
        return self._with(await self._context())

    def _instructions(self) -> list[str]:
        try:
            return self._with(run_sync(self._context()))
        except TypeError as exc:
            warn("read the context without await", exc)
            return list(self.own_instructions)

    async def _context(self) -> str:
        try:
            prompt = await read_prompt(self.conversation, self.agent_memory)
            text = join_instructions(prompt.system, prompt.turn)
            if text and prompt.context is not None:
                mark_injected(self.conversation, prompt.context)
            return text
        except Exception as exc:
            warn("place the context", exc)
            return ""

    def pre_hook(self, run_input: Any) -> None:
        """Agno's pre-hook: records the run's input as the customer's turn."""
        customer_turn(self.conversation, _text(getattr(run_input, "input_content", None)))

    def post_hook(self, run_output: Any) -> None:
        """Agno's post-hook: records the run's answer as the agent's turn, with its usage."""
        try:
            metrics = getattr(run_output, "metrics", None)
            read = getattr(metrics, "cache_read_tokens", 0) or 0
            written = getattr(metrics, "cache_write_tokens", 0) or 0
            usage = model_usage(
                getattr(run_output, "model_provider", None),
                getattr(run_output, "model", None),
                getattr(metrics, "input_tokens", None),
                read,
                written,
            )
            agent_turn(self.conversation, _text(run_output), usage=usage)
        except Exception as exc:
            warn("record the agent's turn", exc)


def _tools(conversation: AnySession, agent_memory: AgentMemoryLike) -> list[Any]:
    try:
        return history_tools(conversation, agent_memory)
    except Exception as exc:
        warn("build the history tools", exc)
        return []
