"""Strands Agents: the customer's memory as a hook provider and tools.

```python
from strands import Agent
from niadra import AsyncNiadra, phone
from niadra.integrations.strands import NiadraHooks

niadra = AsyncNiadra(channel="chat")
conversation = niadra.conversation(session_id, subject=phone(caller))
memory = NiadraHooks(conversation)
agent = Agent(system_prompt="You are Acme's agent.", tools=memory.tools, hooks=[memory])
result = await agent.invoke_async(text)
```

- **Context.** Before each model call the agent's system prompt gets the pack (after the agent's
  own notes, with `agent_memory=`) and then the turn block, as text blocks after your own
  (cache points included); after the call your system prompt is restored, so nothing piles up
  and your prompt stays the cacheable prefix.
- **Turns.** The user's message is recorded when an invocation starts and each model answer when
  its call ends, with the usage of that call (the agent's accumulated usage before and after it)
  and the model id.
- **Tools.** `tools` are the history tools as Strands tools with the kit's names, descriptions
  and JSON Schemas, bound to the customer.
- **Handoff.** `transferred_to_agent()` and `transferred_to_human()` record a transfer; call them
  from your swarm or graph where the conversation changes hands.

Strands' own `MemoryStore` stores the agent's session; Niadra is not that store, so it plugs in
through hooks, next to any session manager you use.
"""

from __future__ import annotations

from typing import Any

try:
    from strands.hooks import (
        AfterInvocationEvent,
        AfterModelCallEvent,
        BeforeInvocationEvent,
        BeforeModelCallEvent,
        HookProvider,
        HookRegistry,
    )
    from strands.tools.tools import PythonAgentTool
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("Strands Agents is not installed: pip install 'niadra[strands]'") from exc

from niadra.integrations._common import (
    AgentMemoryLike,
    AnyKit,
    AnySession,
    agent_turn,
    call_tool,
    customer_turn,
    handoff,
    mark_injected,
    memory_kit_of,
    memory_option,
    model_usage,
    read_prompt,
    tool_specs,
    warn,
)

__all__ = ["NiadraHooks", "history_tools"]


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as Strands tools bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [
        PythonAgentTool(
            spec.name,
            {"name": spec.name, "description": spec.description, "inputSchema": {"json": spec.parameters}},
            _run(kit, spec.name),
        )
        for spec in tool_specs(kit.definitions)
    ]


def _run(kit: AnyKit, name: str) -> Any:
    async def run(tool_use: Any, **_: Any) -> dict[str, Any]:
        text = await call_tool(kit, name, tool_use.get("input") or {})
        return {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": text}]}

    return run


def _text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    return "".join(b["text"] for b in content or [] if isinstance(b, dict) and isinstance(b.get("text"), str))


def _usage(agent: Any) -> dict[str, int]:
    try:
        usage = agent.event_loop_metrics.accumulated_usage
        return {
            k: int(usage.get(k, 0) or 0)
            for k in ("inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
        }
    except Exception:
        return {}


class NiadraHooks(HookProvider):  # type: ignore[misc]
    """Hooks and tools that wire one Niadra conversation into a Strands agent."""

    def __init__(
        self, conversation: AnySession, *, history_tools: bool = True, agent_memory: AgentMemoryLike = None
    ) -> None:
        self.conversation = conversation
        self.agent_memory = memory_option(agent_memory)
        self.tools: list[Any] = _tools(conversation, agent_memory) if history_tools else []
        self._own: Any = None
        self._placed = False
        self._before: dict[str, int] = {}
        self._pending: tuple[str, Any] | None = None

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._invocation)
        registry.add_callback(BeforeModelCallEvent, self._before_model)
        registry.add_callback(AfterModelCallEvent, self._after_model)
        registry.add_callback(AfterInvocationEvent, lambda event: self._settle(event.agent))

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    def _invocation(self, event: Any) -> None:
        for message in event.messages or []:
            if isinstance(message, dict) and message.get("role") == "user":
                customer_turn(self.conversation, _text(message))

    async def _before_model(self, event: Any) -> None:
        agent = event.agent
        self._settle(agent)
        self._before = _usage(agent)
        try:
            prompt = await read_prompt(self.conversation, self.agent_memory)
            if not (prompt.system or prompt.turn):
                return
            self._own = agent.system_prompt_content
            extra = [{"text": text} for text in (prompt.system, prompt.turn) if text]
            agent.system_prompt = [*(self._own or []), *extra]
            self._placed = True
            if prompt.context is not None:
                mark_injected(self.conversation, prompt.context)
        except Exception as exc:
            warn("place the context", exc)

    def _after_model(self, event: Any) -> None:
        agent = event.agent
        if self._placed:
            self._placed = False
            try:
                agent.system_prompt = self._own
            except Exception as exc:
                warn("restore the system prompt", exc)
        response = getattr(event, "stop_response", None)
        if response is not None:
            # Strands adds this call's usage to the agent's metrics just after this event: the turn
            # is recorded at the next model call or when the invocation ends, with the usage in.
            self._pending = (_text(response.message), dict(self._before))

    def _settle(self, agent: Any) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        said, before = pending
        try:
            after = _usage(agent)
            delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
            read, written = delta.get("cacheReadInputTokens", 0), delta.get("cacheWriteInputTokens", 0)
            config = agent.model.get_config() if hasattr(agent.model, "get_config") else {}
            model = config.get("model_id") if isinstance(config, dict) else None
            usage = model_usage(None, model, delta.get("inputTokens", 0) + read + written, read, written)
            agent_turn(self.conversation, said, usage=usage if delta.get("inputTokens") else None)
        except Exception as exc:
            warn("record the agent's turn", exc)


def _tools(conversation: AnySession, agent_memory: AgentMemoryLike) -> list[Any]:
    try:
        return history_tools(conversation, agent_memory)
    except Exception as exc:
        warn("build the history tools", exc)
        return []
