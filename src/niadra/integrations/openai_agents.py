"""OpenAI Agents SDK: the customer's memory in `Runner.run`, through the SDK's own extension points.

```python
from agents import Agent, Runner
from niadra import AsyncNiadra, phone
from niadra.integrations.openai_agents import NiadraAgentsMemory

niadra = AsyncNiadra(channel="chat")

async with niadra.conversation(thread_id, subject=phone(caller)) as conversation:
    memory = NiadraAgentsMemory(conversation)
    agent = Agent(name="Support", instructions="You are Acme's agent.", tools=memory.tools)
    result = await Runner.run(agent, text, hooks=memory.hooks, run_config=memory.run_config())
```

- **Context.** `run_config()` sets `call_model_input_filter`, which runs right before every model
  call: the pack goes after the agent's instructions and the turn block after the input, as a
  system message. A filter you already had runs first.
- **Turns.** `hooks` (a `RunHooks`) records the customer's new messages when the model is first
  called for them, and the agent's answer with the usage the model reported when the call
  returns. Customer turns are keyed by their position in the conversation, so the history a
  `Session` replays on the next run is never recorded twice.
- **Tools.** `tools` are the three history tools as `FunctionTool`s with the kit's names,
  descriptions and JSON Schemas, bound to the customer.
- **Handoff.** An SDK handoff between agents records `handoff("agent")`; give every agent of the
  run the same `memory`.
- **Verification.** What your app proved (a login, an OTP) goes to `conversation.verify()` before
  the run; the next context is read at the new level.

This adapter does not implement the SDK's `Session`: a `Session` stores the agent's own items,
and Niadra keeps derived memory, not a copy of each item. Use any `Session` next to it.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

try:
    from agents import FunctionTool, RunConfig, RunHooks
    from agents.run_config import CallModelData, ModelInputData
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("The OpenAI Agents SDK is not installed: pip install 'niadra[openai-agents]'") from exc

from niadra.integrations._common import (
    AnyKit,
    AnySession,
    agent_turn,
    blocks,
    call_tool,
    customer_turn,
    handoff,
    join_instructions,
    kit_of,
    mark_injected,
    maybe_await,
    model_usage,
    read_context,
    tool_specs,
    warn,
)

__all__ = ["NiadraAgentsMemory", "history_tools"]


def history_tools(conversation: AnySession) -> list[Any]:
    """The history tools as `FunctionTool`s bound to the conversation's customer."""
    kit = kit_of(conversation)
    if kit is None:
        return []
    return [
        FunctionTool(
            name=spec.name,
            description=spec.description,
            params_json_schema=spec.parameters,
            on_invoke_tool=_invoke(kit, spec.name),
            # The kit's schemas keep optional filters optional, which strict mode would forbid.
            strict_json_schema=False,
        )
        for spec in tool_specs(kit.definitions)
    ]


def _invoke(kit: AnyKit, name: str) -> Callable[[Any, str], Any]:
    async def invoke(_context: Any, arguments: str) -> str:
        return await call_tool(kit, name, arguments)

    return invoke


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)


def _text(content: Any) -> str | None:
    """The text of a message's content: a plain string, or its text parts joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts = [_field(part, "text") for part in content]
        joined = "\n".join(part for part in parts if isinstance(part, str) and part)
        return joined or None
    return None


class NiadraAgentsMemory:
    """Wires one Niadra conversation into OpenAI Agents SDK runs. See the module documentation."""

    def __init__(self, conversation: AnySession, *, history_tools: bool = True) -> None:
        self.conversation = conversation
        self.tools: list[Any] = _tools(conversation) if history_tools else []
        self.hooks = _Hooks(self)

    def run_config(self, config: Any = None) -> Any:
        """`config` (or a new `RunConfig`) with the context filter in front of every model call."""
        base = config if config is not None else RunConfig()
        previous = base.call_model_input_filter

        async def model_input(data: CallModelData[Any]) -> ModelInputData:
            if previous is not None:
                data = dataclasses.replace(data, model_data=await maybe_await(previous(data)))
            return await self.input_filter(data)

        return dataclasses.replace(base, call_model_input_filter=model_input)

    async def input_filter(self, data: CallModelData[Any]) -> ModelInputData:
        """The model input with the pack after the instructions and the turn block at the end."""
        model_data = data.model_data
        try:
            context = await read_context(self.conversation)
            if context is None:
                return model_data
            system_block, turn_block = blocks(context)
            items = list(model_data.input)
            if turn_block:
                items.append({"role": "system", "content": turn_block})
            mark_injected(self.conversation, context)
            return ModelInputData(
                input=items, instructions=join_instructions(model_data.instructions, system_block)
            )
        except Exception as exc:
            warn("place the context", exc)
            return model_data

    def record_customer(self, items: Sequence[Any]) -> None:
        """Records the customer's messages among `items`, keyed by their position in the conversation."""
        position = 0
        for item in items:
            if _field(item, "role") != "user":
                continue
            position += 1
            said = _text(_field(item, "content"))
            if said:
                digest = hashlib.sha256(said.encode()).hexdigest()[:12]
                key = f"{self.conversation.id}:customer:{position}:{digest}"
                customer_turn(self.conversation, said, idempotency_key=key)


def _tools(conversation: AnySession) -> list[Any]:
    try:
        return history_tools(conversation)
    except Exception as exc:
        warn("build the history tools", exc)
        return []


class _Hooks(RunHooks[Any]):  # type: ignore[misc]
    def __init__(self, memory: NiadraAgentsMemory) -> None:
        super().__init__()
        self._memory = memory

    async def on_llm_start(
        self, context: Any, agent: Any, system_prompt: str | None, input_items: list[Any]
    ) -> None:
        self._memory.record_customer(input_items)

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        texts = [
            _text(_field(item, "content"))
            for item in response.output
            if _field(item, "type") == "message" and _field(item, "role") == "assistant"
        ]
        said = "\n".join(text for text in texts if text)
        if not said:
            return
        usage = response.usage
        details = getattr(usage, "input_tokens_details", None)
        model = agent.model if isinstance(agent.model, str) else getattr(agent.model, "model", None)
        extra: dict[str, Any] = {}
        if response.response_id:
            extra["idempotency_key"] = f"{self._memory.conversation.id}:agent:{response.response_id}"
        agent_turn(
            self._memory.conversation,
            said,
            usage=model_usage(
                None,
                model,
                getattr(usage, "input_tokens", None),
                getattr(details, "cached_tokens", 0),
                getattr(details, "cache_write_tokens", 0),
            ),
            **extra,
        )

    async def on_handoff(self, context: Any, from_agent: Any, to_agent: Any) -> None:
        handoff(self._memory.conversation, "agent", reason=f"{from_agent.name} to {to_agent.name}")
