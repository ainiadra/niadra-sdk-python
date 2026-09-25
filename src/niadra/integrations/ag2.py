"""AG2 (AutoGen): the customer's memory in an AG2 agent, as middleware and tools.

```python
from ag2 import Agent
from ag2.config import OpenAIConfig
from niadra import AsyncNiadra, phone
from niadra.integrations.ag2 import NiadraAG2

niadra = AsyncNiadra(channel="chat")
conversation = niadra.conversation(thread_id, subject=phone(caller))
memory = NiadraAG2(conversation)
agent = Agent(
    "acme",
    "You are Acme's agent.",
    config=OpenAIConfig(model="gpt-4.1"),
    tools=memory.tools,
    middleware=[memory.middleware],
)
reply = await agent.ask(text)
```

- **Context.** Before every model call the middleware adds one system prompt fragment after the
  agent's own: the agent's own notes (with `agent_memory=`), the pack and the turn block. It is
  taken out again after the call, so the fragment never piles up in the conversation's prompt.
- **Turns.** The request that starts a turn is the customer's; the final answer of the turn is
  the agent's, with the model's usage (input tokens, cache reads and writes).
- **Tools.** `tools` are AG2 function tools with the kit's names, descriptions and JSON Schemas,
  bound to the customer.
- **Handoff.** `transferred_to_agent()` and `transferred_to_human()` record a transfer, for a
  network of agents that hands over or an escalation to a person.

This is the adapter for AG2 1.x (`pip install ag2`), the community continuation of AutoGen; for
Microsoft's line, AutoGen's successor is the Agent Framework (`niadra.integrations.agent_framework`).
Works with `Niadra` and `AsyncNiadra` conversations. Niadra slow or down never stops the agent:
it runs without the context and a tool answers that the history is unavailable.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Sequence
from typing import Any

try:
    from ag2 import Middleware, tool
    from ag2.events import ModelRequest, ModelResponse, TextInput
    from ag2.middleware.base import BaseMiddleware
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("AG2 is not installed: pip install 'niadra[ag2]'") from exc

from niadra.integrations._common import (
    AgentMemoryLike,
    AnyKit,
    AnySession,
    Prompt,
    ToolSpec,
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

__all__ = ["NiadraAG2", "history_tools"]


def _function(kit: AnyKit, spec: ToolSpec) -> Any:
    async def run(**arguments: Any) -> str:
        given = {key: value for key, value in arguments.items() if value is not None}
        return await call_tool(kit, spec.name, given)

    # AG2 calls a tool with the arguments its signature names; the kit's schema names them.
    run.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Any)
            for name in spec.parameters.get("properties", {})
        ]
    )
    run.__name__ = spec.name
    return run


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as AG2 function tools bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [
        tool(
            _function(kit, spec),
            name=spec.name,
            description=spec.description,
            schema=copy.deepcopy(spec.parameters),
        )
        for spec in tool_specs(kit.definitions)
    ]


def _request_text(event: Any) -> str | None:
    if not isinstance(event, ModelRequest):
        return None
    texts = [part.content for part in event.parts if isinstance(part, TextInput)]
    return "\n".join(texts) if texts else None


def _tokens(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _usage(response: Any) -> Any:
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    return model_usage(
        getattr(response, "provider", None),
        getattr(response, "model", None),
        _tokens(prompt_tokens) if prompt_tokens is not None else None,
        _tokens(getattr(usage, "cache_read_input_tokens", 0)),
        _tokens(getattr(usage, "cache_creation_input_tokens", 0)),
    )


class NiadraAG2:
    """The middleware and the tools that wire one Niadra conversation into an AG2 agent."""

    def __init__(
        self,
        conversation: AnySession,
        *,
        history_tools: bool = True,
        agent_memory: AgentMemoryLike = None,
    ) -> None:
        self.conversation = conversation
        self.agent_memory = memory_option(agent_memory)
        self.tools: list[Any] = _tools(conversation, agent_memory) if history_tools else []
        self.middleware: Any = Middleware(_NiadraMiddleware, memory=self)

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    async def prompt(self) -> Prompt:
        """The notes and the context for this model call, stamped as injected; empty on any failure."""
        try:
            prompt = await read_prompt(self.conversation, self.agent_memory)
        except Exception as exc:
            warn("read the context", exc)
            return Prompt()
        if prompt.context is not None and (prompt.system or prompt.turn):
            mark_injected(self.conversation, prompt.context)
        return prompt


class _NiadraMiddleware(BaseMiddleware):  # type: ignore[misc]
    def __init__(self, event: Any, context: Any, memory: NiadraAG2) -> None:
        super().__init__(event, context)
        self.memory = memory

    async def on_turn(self, call_next: Any, event: Any, context: Any) -> Any:
        customer_turn(self.memory.conversation, _request_text(event))
        response = await call_next(event, context)
        try:
            if isinstance(response, ModelResponse) and not response.tool_calls:
                agent_turn(self.memory.conversation, response.content, usage=_usage(response))
        except Exception as exc:
            warn("record the agent's turn", exc)
        return response

    async def on_llm_call(self, call_next: Any, events: Sequence[Any], context: Any) -> Any:
        prompt = await self.memory.prompt()
        block = join_instructions(prompt.system, prompt.turn)
        if not block:
            return await call_next(events, context)
        saved = list(context.prompt)
        context.prompt.append(block)
        try:
            return await call_next(events, context)
        finally:
            context.prompt[:] = saved


def _tools(conversation: AnySession, agent_memory: AgentMemoryLike) -> list[Any]:
    try:
        return history_tools(conversation, agent_memory)
    except Exception as exc:
        warn("build the history tools", exc)
        return []
