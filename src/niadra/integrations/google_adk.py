"""Google Agent Development Kit (ADK): the customer's memory through the agent's model callbacks.

```python
from google.adk.agents import LlmAgent
from niadra import AsyncNiadra, phone
from niadra.integrations.google_adk import NiadraADK

niadra = AsyncNiadra(channel="chat")
conversation = niadra.conversation(session_id, subject=phone(caller))
memory = NiadraADK(conversation)
agent = LlmAgent(
    name="support",
    model="gemini-2.5-flash",
    instruction="You are Acme's agent.",
    tools=memory.tools,
    before_model_callback=memory.before_model,
    after_model_callback=memory.after_model,
)
```

- **Context.** `before_model` appends the pack (after the agent's own notes, with
  `agent_memory=`) to the request's system instruction, after the agent's own instruction, and
  the turn block as a user part after the contents.
- **Turns.** The invocation's user message is recorded once (keyed by the invocation id, however
  many model calls the invocation makes), and each final model answer with Gemini's usage.
- **Tools.** `tools` are ADK `BaseTool`s whose declarations carry the kit's names, descriptions
  and JSON Schemas, bound to the customer.
- **Handoff.** A `transfer_to_agent` call in the model's answer records `handoff("agent")`;
  `transferred_to_human()` records a transfer to a person.

If the agent has callbacks of its own, pass lists: ADK runs them in order.
"""

from __future__ import annotations

from typing import Any

try:
    from google.adk.tools import BaseTool
    from google.genai import types
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("Google ADK is not installed: pip install 'niadra[google-adk]'") from exc

from niadra.integrations._common import (
    AgentMemoryLike,
    AnyKit,
    AnySession,
    ToolSpec,
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

__all__ = ["NiadraADK", "NiadraTool", "history_tools"]


class NiadraTool(BaseTool):  # type: ignore[misc]
    """One history tool as an ADK tool: the kit's declaration, the kit's call."""

    def __init__(self, kit: AnyKit, spec: ToolSpec) -> None:
        super().__init__(name=spec.name, description=spec.description)
        self._kit = kit
        self._spec = spec

    def _get_declaration(self) -> Any:
        return types.FunctionDeclaration(
            name=self._spec.name,
            description=self._spec.description,
            parameters_json_schema=self._spec.parameters,
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        return await call_tool(self._kit, self._spec.name, args)


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as ADK tools bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [NiadraTool(kit, spec) for spec in tool_specs(kit.definitions)]


def _text(content: Any) -> str:
    parts = getattr(content, "parts", None) or []
    return "".join(
        p.text
        for p in parts
        if isinstance(getattr(p, "text", None), str) and not getattr(p, "thought", False)
    )


class NiadraADK:
    """The callbacks and tools that wire one Niadra conversation into an ADK agent."""

    def __init__(
        self, conversation: AnySession, *, history_tools: bool = True, agent_memory: AgentMemoryLike = None
    ) -> None:
        self.conversation = conversation
        self.agent_memory = memory_option(agent_memory)
        self.tools: list[Any] = _tools(conversation, agent_memory) if history_tools else []
        self._seen: set[str] = set()

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    async def before_model(self, callback_context: Any, llm_request: Any) -> None:
        """ADK's `before_model_callback`: records the user's message and places the context."""
        try:
            invocation = str(getattr(callback_context, "invocation_id", "") or "")
            if invocation and invocation not in self._seen:
                self._seen.add(invocation)
                customer_turn(
                    self.conversation,
                    _text(getattr(callback_context, "user_content", None)),
                    idempotency_key=f"{self.conversation.id}:customer:{invocation}",
                )
            prompt = await read_prompt(self.conversation, self.agent_memory)
            if prompt.system:
                llm_request.append_instructions([prompt.system])
            if prompt.turn:
                llm_request.contents.append(types.Content(role="user", parts=[types.Part(text=prompt.turn)]))
            if prompt.context is not None and (prompt.system or prompt.turn):
                mark_injected(self.conversation, prompt.context)
        except Exception as exc:
            warn("place the context", exc)

    async def after_model(self, callback_context: Any, llm_response: Any) -> None:
        """ADK's `after_model_callback`: records the final answer and any transfer between agents."""
        try:
            if getattr(llm_response, "partial", False):
                return
            content = getattr(llm_response, "content", None)
            for part in getattr(content, "parts", None) or []:
                call = getattr(part, "function_call", None)
                if call is not None and call.name == "transfer_to_agent":
                    target = (call.args or {}).get("agent_name")
                    handoff(self.conversation, "agent", reason=f"transfer to {target}" if target else None)
            usage = getattr(llm_response, "usage_metadata", None)
            version = getattr(llm_response, "model_version", None)
            reported = model_usage(
                "google",
                version if isinstance(version, str) else None,
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "cached_content_token_count", None) or 0,
            )
            agent_turn(self.conversation, _text(content), usage=reported)
        except Exception as exc:
            warn("record the agent's turn", exc)


def _tools(conversation: AnySession, agent_memory: AgentMemoryLike) -> list[Any]:
    try:
        return history_tools(conversation, agent_memory)
    except Exception as exc:
        warn("build the history tools", exc)
        return []
