"""CrewAI: the customer's memory in a crew, through its kickoff callbacks and tools.

```python
from crewai import Agent, Crew, Task
from niadra import Niadra, phone
from niadra.integrations.crewai import NiadraCrew

niadra = Niadra(channel="chat")
conversation = niadra.conversation(thread_id, subject=phone(caller))
memory = NiadraCrew(conversation)
support = Agent(role="Support", goal="Help the customer", backstory="...", tools=memory.tools)
answer = Task(
    description="Answer the customer: {message}\\n\\n{niadra_context}", expected_output="...", agent=support
)
crew = Crew(
    agents=[support],
    tasks=[answer],
    before_kickoff_callbacks=[memory.before_kickoff],
    after_kickoff_callbacks=[memory.after_kickoff],
)
crew.kickoff(inputs={"message": text})
```

- **Context.** `before_kickoff` adds `niadra_context` to the crew's inputs: the agent's own notes
  (with `agent_memory=`), the pack and the turn block. Place `{niadra_context}` in a task's
  description or an agent's backstory, after your own text.
- **Turns.** The input under `message` (or `message_key=`) is the customer's turn, recorded
  before the crew starts; the crew's final answer is the agent's turn, recorded when it ends.
- **Tools.** `tools` are CrewAI tools whose argument schema is the kit's JSON Schema, bound to the
  customer.
- **Handoff.** `transferred_to_agent()` and `transferred_to_human()` record a transfer.

The crew's own memory (short-term, long-term, entity) stays CrewAI's; Niadra is the company's
memory of the customer, shared with every other agent.
"""

from __future__ import annotations

import copy
from typing import Any

try:
    from crewai.tools import BaseTool
    from pydantic import BaseModel, ConfigDict, PrivateAttr
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("CrewAI is not installed: pip install 'niadra[crewai]'") from exc

from niadra.integrations._common import (
    AgentMemoryLike,
    AnyKit,
    AnySession,
    ToolSpec,
    agent_turn,
    call_tool,
    customer_turn,
    handoff,
    join_instructions,
    mark_injected,
    memory_kit_of,
    memory_option,
    read_prompt,
    run_sync,
    tool_specs,
    warn,
)

__all__ = ["NiadraCrew", "NiadraTool", "history_tools"]

CONTEXT_INPUT = "niadra_context"


def _arguments(spec: ToolSpec) -> type[BaseModel]:
    """An argument model that takes what the kit takes and reports the kit's JSON Schema as its own."""

    class Arguments(BaseModel):
        model_config = ConfigDict(extra="allow")

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return copy.deepcopy(spec.parameters)

    Arguments.__name__ = Arguments.__qualname__ = (
        "".join(p.title() for p in spec.name.split("_")) + "Arguments"
    )
    return Arguments


class NiadraTool(BaseTool):  # type: ignore[misc]
    """One history tool as a CrewAI tool, bound to the customer."""

    _kit: Any = PrivateAttr()

    def __init__(self, kit: AnyKit, spec: ToolSpec) -> None:
        super().__init__(name=spec.name, description=spec.description, args_schema=_arguments(spec))
        self._kit = kit

    def _run(self, **arguments: Any) -> str:
        try:
            return run_sync(call_tool(self._kit, self.name, arguments))
        except TypeError as exc:
            warn(f"run {self.name} without await", exc)
            return '{"error": "use the async tool with an AsyncNiadra conversation"}'

    async def _arun(self, **arguments: Any) -> str:
        return await call_tool(self._kit, self.name, arguments)


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as CrewAI tools bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [NiadraTool(kit, spec) for spec in tool_specs(kit.definitions)]


class NiadraCrew:
    """Kickoff callbacks and tools that wire one Niadra conversation into a crew."""

    def __init__(
        self,
        conversation: AnySession,
        *,
        message_key: str = "message",
        history_tools: bool = True,
        agent_memory: AgentMemoryLike = None,
    ) -> None:
        self.conversation = conversation
        self.message_key = message_key
        self.agent_memory = memory_option(agent_memory)
        self.tools: list[Any] = _tools(conversation, agent_memory) if history_tools else []

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    def before_kickoff(self, inputs: dict[str, Any] | None) -> dict[str, Any]:
        """CrewAI's `before_kickoff_callbacks`: records the message and adds `niadra_context`."""
        values = dict(inputs or {})
        message = values.get(self.message_key)
        customer_turn(self.conversation, message if isinstance(message, str) else None)
        text = ""
        try:
            prompt = run_sync(read_prompt(self.conversation, self.agent_memory))
            text = join_instructions(prompt.system, prompt.turn)
            if text and prompt.context is not None:
                mark_injected(self.conversation, prompt.context)
        except Exception as exc:
            warn("place the context", exc)
        values[CONTEXT_INPUT] = text
        return values

    def after_kickoff(self, output: Any) -> Any:
        """CrewAI's `after_kickoff_callbacks`: records the crew's final answer as the agent's turn."""
        raw = getattr(output, "raw", None)
        agent_turn(self.conversation, raw if isinstance(raw, str) else None)
        return output


def _tools(conversation: AnySession, agent_memory: AgentMemoryLike) -> list[Any]:
    try:
        return history_tools(conversation, agent_memory)
    except Exception as exc:
        warn("build the history tools", exc)
        return []
