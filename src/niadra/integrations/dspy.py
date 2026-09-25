"""DSPy: the customer's memory in a DSPy program, through its adapter, a module wrapper and tools.

```python
import dspy
from niadra import Niadra, phone
from niadra.integrations.dspy import NiadraModule, history_tools

niadra = Niadra(channel="chat")
conversation = niadra.conversation(thread_id, subject=phone(caller))
react = dspy.ReAct("question -> answer", tools=history_tools(conversation))
agent = NiadraModule(react, conversation, input_field="question", output_field="answer")
prediction = agent(question=text)
```

- **Context.** DSPy builds the prompt in its adapter, and `niadra_adapter()` is the adapter you
  use (`ChatAdapter` by default, or `base=dspy.JSONAdapter`) with one change in `format()`: the
  pack (after the agent's own notes, with `agent_memory=`) goes right after the system message
  DSPy writes from the signature, and the turn block at the end. `NiadraModule` runs your program
  with that adapter; `dspy.context(adapter=niadra_adapter(conversation))` does the same around
  any call. The pack is not a signature field, so an optimizer never sees it as an input.
- **Turns.** `NiadraModule` records the input field as the customer's turn and the output field
  as the agent's, with the usage DSPy tracked (with `dspy.configure(track_usage=True)`).
- **Tools.** `history_tools()` gives `dspy.Tool`s with the kit's names, descriptions and argument
  schemas, bound to the customer; in native function calling they are the kit's definitions,
  word for word.

Works with `Niadra` and `AsyncNiadra` conversations (with `AsyncNiadra`, call the program with
`acall`). Niadra slow or down never stops the program: it runs without the context and a tool
answers that the history is unavailable.
"""

from __future__ import annotations

import copy
from typing import Any

try:
    import dspy
    from dspy.adapters.chat_adapter import ChatAdapter
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("DSPy is not installed: pip install 'niadra[dspy]'") from exc

from niadra.integrations._common import (
    AgentMemoryLike,
    AgentMemoryOption,
    AnyKit,
    AnySession,
    Prompt,
    ToolSpec,
    agent_turn,
    call_tool,
    call_tool_sync,
    customer_turn,
    handoff,
    inject_prompt,
    mark_injected,
    memory_kit_of,
    memory_option,
    model_usage,
    read_prompt,
    run_sync,
    tool_specs,
    warn,
)
from niadra.tools import AsyncToolKit, ToolKit

__all__ = ["NiadraModule", "NiadraTool", "history_tools", "niadra_adapter"]


class NiadraTool(dspy.Tool):  # type: ignore[misc]
    """A history tool: a `dspy.Tool` whose function-calling definition is the kit's, word for word."""

    def __init__(self, kit: AnyKit, spec: ToolSpec) -> None:
        super().__init__(
            _function(kit, spec.name),
            name=spec.name,
            desc=spec.description,
            args=copy.deepcopy(spec.parameters.get("properties", {})),
        )
        self._definition = {
            "type": "function",
            "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
        }

    def format_as_litellm_function_call(self) -> dict[str, Any]:
        return copy.deepcopy(self._definition)


def _function(kit: AnyKit, name: str) -> Any:
    if isinstance(kit, AsyncToolKit):

        async def arun(**arguments: Any) -> str:
            return await call_tool(kit, name, arguments)

        return arun

    sync_kit: ToolKit = kit

    def run(**arguments: Any) -> str:
        return call_tool_sync(sync_kit, name, arguments)

    return run


def history_tools(conversation: AnySession, agent_memory: AgentMemoryLike = None) -> list[Any]:
    """The history tools as `dspy.Tool`s bound to the conversation's customer."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return []
    return [NiadraTool(kit, spec) for spec in tool_specs(kit.definitions)]


class _NiadraFormat:
    """What `niadra_adapter()` adds to an adapter: the context read for the call, placed by `format()`."""

    _niadra_conversation: AnySession
    _niadra_option: AgentMemoryOption | None
    _niadra_prompt: Prompt | None = None

    def _niadra_stamp(self, prompt: Prompt) -> Prompt:
        if prompt.context is not None and (prompt.system or prompt.turn):
            mark_injected(self._niadra_conversation, prompt.context)
        return prompt

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        try:
            prompt = self._niadra_stamp(run_sync(read_prompt(self._niadra_conversation, self._niadra_option)))
        except TypeError as exc:
            warn("read the context without await", exc)
            prompt = Prompt()
        except Exception as exc:
            warn("read the context", exc)
            prompt = Prompt()
        self._niadra_prompt = prompt
        try:
            return super().__call__(*args, **kwargs)  # type: ignore[misc]
        finally:
            self._niadra_prompt = None

    async def acall(self, *args: Any, **kwargs: Any) -> Any:
        try:
            prompt = self._niadra_stamp(await read_prompt(self._niadra_conversation, self._niadra_option))
        except Exception as exc:
            warn("read the context", exc)
            prompt = Prompt()
        self._niadra_prompt = prompt
        try:
            return await super().acall(*args, **kwargs)  # type: ignore[misc]
        finally:
            self._niadra_prompt = None

    def format(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = super().format(*args, **kwargs)  # type: ignore[misc]
        prompt = self._niadra_prompt
        if prompt is None or not (prompt.system or prompt.turn):
            return messages
        try:
            return inject_prompt(prompt, messages)
        except Exception as exc:
            warn("place the context", exc)
            return messages


_ADAPTERS: dict[type[Any], type[Any]] = {}


def niadra_adapter(
    conversation: AnySession,
    *,
    base: type[Any] = ChatAdapter,
    agent_memory: AgentMemoryLike = None,
    **options: Any,
) -> Any:
    """An instance of `base` (a DSPy adapter class) that places the conversation's context."""
    cls = _ADAPTERS.get(base)
    if cls is None:
        cls = _ADAPTERS[base] = type(f"Niadra{base.__name__}", (_NiadraFormat, base), {})
    adapter = cls(**options)
    adapter._niadra_conversation = conversation
    adapter._niadra_option = memory_option(agent_memory)
    return adapter


def _usage(prediction: Any) -> Any:
    """The usage of the program's last model, when DSPy tracked it."""
    try:
        tracked = prediction.get_lm_usage() or {}
    except Exception:
        return None
    if not tracked:
        return None
    model, counts = list(tracked.items())[-1]
    details = counts.get("prompt_tokens_details") or {}
    return model_usage(None, str(model), counts.get("prompt_tokens"), details.get("cached_tokens") or 0)


class NiadraModule(dspy.Module):  # type: ignore[misc]
    """Runs a DSPy program with the conversation's context, and records the turn it answers."""

    def __init__(
        self,
        program: Any,
        conversation: AnySession,
        *,
        input_field: str = "question",
        output_field: str = "answer",
        agent_memory: AgentMemoryLike = None,
        base: type[Any] = ChatAdapter,
    ) -> None:
        super().__init__()
        self.program = program
        self.conversation = conversation
        self.input_field = input_field
        self.output_field = output_field
        self.adapter = niadra_adapter(conversation, base=base, agent_memory=agent_memory)

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    def _answered(self, prediction: Any) -> Any:
        said = getattr(prediction, self.output_field, None)
        agent_turn(self.conversation, said if isinstance(said, str) else None, usage=_usage(prediction))
        return prediction

    def forward(self, **kwargs: Any) -> Any:
        customer_turn(self.conversation, _text(kwargs.get(self.input_field)))
        with dspy.context(adapter=self.adapter):
            prediction = self.program(**kwargs)
        return self._answered(prediction)

    async def aforward(self, **kwargs: Any) -> Any:
        customer_turn(self.conversation, _text(kwargs.get(self.input_field)))
        with dspy.context(adapter=self.adapter):
            prediction = await self.program.acall(**kwargs)
        return self._answered(prediction)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None
