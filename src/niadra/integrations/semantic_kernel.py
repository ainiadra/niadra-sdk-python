"""Semantic Kernel: the customer's memory in a kernel, as a plugin, filters and an agent thread.

```python
from semantic_kernel import Kernel
from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion
from niadra import AsyncNiadra, phone
from niadra.integrations.semantic_kernel import NiadraKernel

niadra = AsyncNiadra(channel="chat")
conversation = niadra.conversation(thread_id, subject=phone(caller))
memory = NiadraKernel(conversation)
kernel = memory.register(Kernel())  # the history plugin and the prompt filters
agent = ChatCompletionAgent(
    service=OpenAIChatCompletion(), kernel=kernel, name="acme", instructions="You are Acme's agent."
)
thread = memory.thread()
response = await agent.get_response(messages=text, thread=thread)
```

- **Context.** With a `ChatCompletionAgent`, `memory.thread()` is the agent's thread: every time
  the agent reads it, the pack (after the agent's own notes, with `agent_memory=`) comes first,
  which the agent places right after its instructions, then the stored messages, then the turn
  block. Only the conversation is stored; the Niadra messages are made fresh on every read. With
  a prompt function (`kernel.invoke_prompt`, `kernel.invoke(function, ...)`), the prompt
  rendering filter that `register()` adds places them the same way in the rendered prompt. When
  you call a chat service yourself, `await memory.chat_history(history)` gives a copy with them.
- **Turns.** The thread records the user's messages as the customer's turns and the assistant's
  text as the agent's, with its usage and model; with a prompt function, the filters record the
  last user message of the prompt and the function's answer.
- **Tools.** `memory.plugin` is a `KernelPlugin` named `niadra` whose functions are the history
  tools, with the kit's names, descriptions and JSON Schemas, bound to the customer. Semantic
  Kernel shows the model a function as `plugin-function`: `niadra-search_customer_history`.
- **Handoff.** `transferred_to_agent()` and `transferred_to_human()` record a transfer, for a group
  chat or an orchestration that hands over, or an escalation to a person.

Works with `Niadra` and `AsyncNiadra` conversations. Niadra slow or down never stops the kernel:
the prompt goes out without the context and a tool answers that the history is unavailable.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Any

try:
    from semantic_kernel.agents import ChatHistoryAgentThread
    from semantic_kernel.contents import AuthorRole, ChatHistory, ChatMessageContent, FunctionCallContent
    from semantic_kernel.filters import FilterTypes
    from semantic_kernel.functions import KernelFunctionFromMethod, KernelParameterMetadata, KernelPlugin
    from semantic_kernel.functions import kernel_function as _kernel_function
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("Semantic Kernel is not installed: pip install 'niadra[semantic-kernel]'") from exc

from niadra.integrations._common import (
    MARK,
    AgentMemoryLike,
    AnyKit,
    AnySession,
    Prompt,
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

__all__ = ["NiadraKernel", "NiadraThread", "history_plugin"]

PLUGIN_NAME = "niadra"


def _function(kit: AnyKit, spec: ToolSpec, plugin_name: str) -> Any:
    async def run(**arguments: Any) -> str:
        return await call_tool(kit, spec.name, arguments)

    method = _kernel_function(name=spec.name, description=spec.description)(run)
    required = set(spec.parameters.get("required", []))
    parameters = [
        KernelParameterMetadata(
            name=name,
            description=schema.get("description"),
            type_=str(schema.get("type") or "object"),
            is_required=name in required,
            schema_data=dict(schema),
        )
        for name, schema in spec.parameters.get("properties", {}).items()
    ]
    return KernelFunctionFromMethod(method=method, plugin_name=plugin_name, parameters=parameters)


def history_plugin(
    conversation: AnySession, agent_memory: AgentMemoryLike = None, *, name: str = PLUGIN_NAME
) -> Any:
    """The history tools as a `KernelPlugin` bound to the conversation's customer, or None."""
    kit = memory_kit_of(conversation, memory_option(agent_memory))
    if kit is None:
        return None
    functions = [_function(kit, spec, name) for spec in tool_specs(kit.definitions)]
    return KernelPlugin(name=name, description="The customer's history, from Niadra.", functions=functions)


def _niadra(text: str) -> Any:
    return ChatMessageContent(role=AuthorRole.SYSTEM, content=text, metadata={MARK: True})


def _ours(message: Any) -> bool:
    return bool((getattr(message, "metadata", None) or {}).get(MARK))


def _instructions(messages: list[Any]) -> int:
    count = 0
    while count < len(messages) and messages[count].role in (AuthorRole.SYSTEM, AuthorRole.DEVELOPER):
        count += 1
    return count


def _answer(message: Any) -> str | None:
    """The assistant's text, or None for a tool call or anything that is not the assistant's."""
    if getattr(message, "role", None) != AuthorRole.ASSISTANT:
        return None
    if any(isinstance(item, FunctionCallContent) for item in getattr(message, "items", []) or []):
        return None
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else None


def _usage(message: Any) -> Any:
    usage = (getattr(message, "metadata", None) or {}).get("usage")
    details = getattr(usage, "prompt_tokens_details", None)
    return model_usage(
        None,
        getattr(message, "ai_model_id", None),
        getattr(usage, "prompt_tokens", None),
        getattr(details, "cached_tokens", 0) or 0,
    )


class NiadraKernel:
    """The plugin, the filters and the thread that wire one Niadra conversation into a kernel."""

    def __init__(
        self,
        conversation: AnySession,
        *,
        history_tools: bool = True,
        agent_memory: AgentMemoryLike = None,
        plugin_name: str = PLUGIN_NAME,
    ) -> None:
        self.conversation = conversation
        self.agent_memory = memory_option(agent_memory)
        self.plugin: Any = _plugin(conversation, agent_memory, plugin_name) if history_tools else None

    def register(self, kernel: Any) -> Any:
        """Adds the plugin and the two filters to the kernel, and returns it."""
        if self.plugin is not None:
            kernel.add_plugin(self.plugin)
        kernel.add_filter(FilterTypes.PROMPT_RENDERING, self.prompt_filter)
        kernel.add_filter(FilterTypes.FUNCTION_INVOCATION, self.function_filter)
        return kernel

    def thread(self, chat_history: Any = None) -> NiadraThread:
        """A thread for a `ChatCompletionAgent`, keyed by the conversation's id."""
        return NiadraThread(self, chat_history=chat_history)

    def transferred_to_agent(self, reason: str | None = None, target_source: str | None = None) -> None:
        """Records that another agent took the conversation over."""
        handoff(self.conversation, "agent", reason=reason, target_source=target_source)

    def transferred_to_human(self, reason: str | None = None) -> None:
        """Records that the conversation went to a person."""
        handoff(self.conversation, "human", reason=reason)

    async def prompt(self) -> Prompt:
        """The notes and the context for this turn, stamped as injected; empty on any failure."""
        try:
            prompt = await read_prompt(self.conversation, self.agent_memory)
        except Exception as exc:
            warn("read the context", exc)
            return Prompt()
        if prompt.context is not None and (prompt.system or prompt.turn):
            mark_injected(self.conversation, prompt.context)
        return prompt

    async def chat_history(self, chat_history: Any) -> Any:
        """A copy of the history with the pack after its instructions and the turn block at the end."""
        messages = [m for m in chat_history.messages if not _ours(m)]
        prompt = await self.prompt()
        if prompt.system:
            messages.insert(_instructions(messages), _niadra(prompt.system))
        if prompt.turn:
            messages.append(_niadra(prompt.turn))
        return ChatHistory(messages=messages)

    async def prompt_filter(self, context: Any, next: Callable[[Any], Awaitable[None]]) -> None:
        """Prompt rendering filter: places the context in the rendered prompt of a prompt function."""
        await next(context)
        try:
            rendered = context.rendered_prompt
            if not rendered:
                return
            history = ChatHistory.from_rendered_prompt(rendered)
            users = [m for m in history.messages if m.role == AuthorRole.USER]
            if users:
                customer_turn(self.conversation, users[-1].content)
            placed = await self.chat_history(history)
            if len(placed.messages) != len(history.messages):
                context.rendered_prompt = placed.to_prompt()
        except Exception as exc:
            warn("place the context", exc)

    async def function_filter(self, context: Any, next: Callable[[Any], Awaitable[None]]) -> None:
        """Function invocation filter: records a prompt function's answer as the agent's turn."""
        await next(context)
        try:
            if not context.function.is_prompt or context.result is None:
                return
            value = context.result.value
            answers = value if isinstance(value, list) else [value]
            for message in answers:
                said = _answer(message)
                if said:
                    agent_turn(self.conversation, said, usage=_usage(message))
        except Exception as exc:
            warn("record the agent's turn", exc)


class NiadraThread(ChatHistoryAgentThread):  # type: ignore[misc]
    """A `ChatCompletionAgent` thread: the stored conversation with the customer's context around it."""

    def __init__(self, memory: NiadraKernel, chat_history: Any = None, thread_id: str | None = None) -> None:
        super().__init__(chat_history=chat_history, thread_id=thread_id or memory.conversation.id)
        self._niadra = memory

    async def get_messages(self) -> AsyncIterable[Any]:
        prompt = await self._niadra.prompt()
        if prompt.system:
            yield _niadra(prompt.system)
        async for message in super().get_messages():
            if not _ours(message):
                yield message
        if prompt.turn:
            yield _niadra(prompt.turn)

    async def _on_new_message(self, new_message: Any) -> None:
        if _ours(new_message):
            return
        await super()._on_new_message(new_message)
        try:
            if isinstance(new_message, str):
                customer_turn(self._niadra.conversation, new_message)
            elif new_message.role == AuthorRole.USER:
                customer_turn(self._niadra.conversation, new_message.content)
            elif (said := _answer(new_message)) is not None:
                agent_turn(self._niadra.conversation, said, usage=_usage(new_message))
        except Exception as exc:
            warn("record a turn", exc)


def _plugin(conversation: AnySession, agent_memory: AgentMemoryLike, name: str) -> Any:
    try:
        return history_plugin(conversation, agent_memory, name=name)
    except Exception as exc:
        warn("build the history tools", exc)
        return None
