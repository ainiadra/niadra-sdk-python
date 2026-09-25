"""LangGraph: the customer's memory in a LangGraph agent, as `create_agent` middleware.

```python
from langchain.agents import create_agent
from niadra import AsyncNiadra, phone
from niadra.integrations.langgraph import NiadraMiddleware

niadra = AsyncNiadra(channel="chat")
async with niadra.conversation(thread_id, subject=phone(caller)) as conversation:
    agent = create_agent("openai:gpt-4.1", tools=[...], middleware=[NiadraMiddleware(conversation)])
    result = await agent.ainvoke({"messages": [{"role": "user", "content": text}]})
```

`NiadraMiddleware` wraps every model call of the agent:

- **Context.** The pack (after the agent's own notes, with `agent_memory=`) is appended to the
  system message, which keeps your instructions first, and the turn block goes after the
  messages. Nothing is written to the graph's state: the checkpointer never stores a pack.
- **Turns.** The customer's messages are recorded before the call, keyed by their position in
  the conversation (the history the checkpointer replays is never recorded twice); the model's
  answer, with its `usage_metadata`, after it.
- **Tools.** The middleware brings the history tools (and the agent memory tools) as its own
  `tools`, so `create_agent` offers them without you listing them.
- **Handoff.** `transferred_to_agent()` and `transferred_to_human()` record a transfer; call them
  where your graph hands the conversation over.

For the older `langgraph.prebuilt.create_react_agent`, pass `pre_model_hook=pre_model_hook(conversation)`:
it gives the model the same messages through `llm_input_messages`, again without touching state.
Record the turns there with `NiadraCallbackHandler` from `niadra.integrations.langchain`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

try:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import SystemMessage
    from langchain_core.runnables import RunnableLambda
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("LangGraph and LangChain are not installed: pip install 'niadra[langgraph]'") from exc

from niadra.integrations._common import (
    AgentMemoryLike,
    AnySession,
    Prompt,
    handoff,
    join_instructions,
    mark_injected,
    memory_option,
    read_prompt,
    run_sync,
    warn,
)
from niadra.integrations.langchain import (
    awith_context,
    history_tools,
    record_answer,
    record_customer,
    with_context,
)

__all__ = ["NiadraMiddleware", "pre_model_hook"]


class NiadraMiddleware(AgentMiddleware):  # type: ignore[misc]
    """`create_agent` middleware that reads and records one Niadra conversation."""

    def __init__(
        self, conversation: AnySession, *, history_tools: bool = True, agent_memory: AgentMemoryLike = None
    ) -> None:
        super().__init__()
        self.conversation = conversation
        self.agent_memory = memory_option(agent_memory)
        self.tools = _tools(conversation, agent_memory) if history_tools else []

    @property
    def name(self) -> str:
        return "NiadraMiddleware"

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        try:
            prompt = run_sync(read_prompt(self.conversation, self.agent_memory))
        except TypeError as exc:
            warn("read the context without await", exc)
            prompt = Prompt()
        response = handler(self._before(request, prompt))
        self._after(response)
        return response

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        prompt = await read_prompt(self.conversation, self.agent_memory)
        response = await handler(self._before(request, prompt))
        self._after(response)
        return response

    def _before(self, request: Any, prompt: Prompt) -> Any:
        try:
            record_customer(self.conversation, request.messages)
            if not (prompt.system or prompt.turn):
                return request
            changes: dict[str, Any] = {}
            if prompt.system:
                own = request.system_message.text if request.system_message is not None else None
                changes["system_message"] = SystemMessage(content=join_instructions(own, prompt.system))
            if prompt.turn:
                changes["messages"] = [*request.messages, SystemMessage(content=prompt.turn)]
            if prompt.context is not None:
                mark_injected(self.conversation, prompt.context)
            return request.override(**changes)
        except Exception as exc:
            warn("place the context", exc)
            return request

    def _after(self, response: Any) -> None:
        try:
            result = getattr(response, "result", None)
            messages = result if isinstance(result, list) else [response]
            for message in messages:
                record_answer(self.conversation, message)
        except Exception as exc:
            warn("record the agent's turn", exc)


def _tools(conversation: AnySession, agent_memory: AgentMemoryLike) -> list[Any]:
    try:
        return history_tools(conversation, agent_memory)
    except Exception as exc:
        warn("build the history tools", exc)
        return []


def pre_model_hook(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> Any:
    """A `pre_model_hook` for `create_react_agent`: the model's input with the context, as
    `llm_input_messages`, so nothing is added to the graph's state."""

    def hook(state: Any) -> dict[str, Any]:
        return {"llm_input_messages": with_context(conversation, _state_messages(state), agent_memory)}

    async def ahook(state: Any) -> dict[str, Any]:
        return {"llm_input_messages": await awith_context(conversation, _state_messages(state), agent_memory)}

    return RunnableLambda(hook, afunc=ahook, name="niadra_context")


def _state_messages(state: Any) -> list[Any]:
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    return list(messages or [])
