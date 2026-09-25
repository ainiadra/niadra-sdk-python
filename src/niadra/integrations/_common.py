"""What every framework adapter shares, so that all of them behave the same way.

- The pinned pack goes right after the agent's own instructions and the turn block goes after
  the conversation, the rule of `wrap()`: instructions stay the cacheable prefix of the prompt.
- The history tools are the kit's definitions, word for word, handed to each framework in its
  own tool shape. The customer is bound by the kit and is never a parameter the model sees.
- Every Niadra call an adapter makes is guarded: a failure is logged without content and the
  agent carries on without the memory. Nothing here raises into the framework.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from niadra.conversation import AsyncConversation, AsyncTask, Conversation, Task
from niadra.handles import phone
from niadra.models.common import Handle
from niadra.models.events import ModelUsage
from niadra.models.results import Context
from niadra.tools import _UNAVAILABLE as UNAVAILABLE
from niadra.tools import BUILTIN_DEFINITIONS, AsyncToolKit, ToolKit
from niadra.usage import provider_of
from niadra.vocabulary import Verification

logger = logging.getLogger("niadra")

T = TypeVar("T")

AnySession = Conversation | Task | AsyncConversation | AsyncTask
AnyKit = ToolKit | AsyncToolKit

INSTRUCTION_ROLES = frozenset({"system", "developer"})

# Marks the messages an adapter adds, where a framework keeps metadata on messages, so a second
# pass over the same prompt does not add them twice.
MARK = "niadra"

_TOOL_FAILED = json.dumps({"error": UNAVAILABLE})


@dataclass(frozen=True)
class ToolSpec:
    """One history tool in the neutral shape every framework can take: name, text, JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


def tool_specs(definitions: Sequence[Mapping[str, Any]] = BUILTIN_DEFINITIONS) -> list[ToolSpec]:
    """The kit's function-calling definitions as `ToolSpec`s, with the same words and schemas."""
    return [
        ToolSpec(
            name=str(d["function"]["name"]),
            description=str(d["function"].get("description", "")),
            parameters=dict(d["function"].get("parameters") or {"type": "object", "properties": {}}),
        )
        for d in definitions
    ]


def warn(action: str, exc: BaseException) -> None:
    """Logs a swallowed failure: the exception's type only, never its message (it may quote content)."""
    logger.warning("niadra: could not %s (%s)", action, type(exc).__name__)


async def maybe_await(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


def run_sync(coroutine: Coroutine[Any, Any, T]) -> T:
    """Runs a coroutine that never suspends, which is what an adapter's code is with a sync client.

    Lets one implementation serve `Niadra` and `AsyncNiadra`: with the sync client every awaited
    value is plain, so the coroutine finishes on its first step.
    """
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value  # type: ignore[no-any-return]
    coroutine.close()
    raise TypeError("this call needs `await`: it was given an AsyncNiadra conversation")


async def read_context(session: AnySession | None, turn: str | None = None) -> Context | None:
    """The session's pack for this turn, or None when there is none or it could not be read.

    `turn` is the customer's turn when the framework has it before the session recorded it; by
    default the session sends the last one `customer()` recorded.
    """
    if session is None:
        return None
    try:
        context = await maybe_await(
            session.context(turn=turn) if turn and turn.strip() else session.context()
        )
    except Exception as exc:
        warn("read the context", exc)
        return None
    return context if (context.system_block or context.turn_block or context.is_holdout) else None


def mark_injected(session: AnySession, context: Context) -> None:
    try:
        session.mark_injected(context)
    except Exception as exc:
        warn("stamp the injection", exc)


def blocks(context: Context | None) -> tuple[str, str]:
    """`(system_block, turn_block)`, both empty without a context."""
    if context is None:
        return "", ""
    return context.system_block, context.turn_block


def instruction_count(messages: Sequence[Any], role: Callable[[Any], Any] | None = None) -> int:
    """How many leading messages are the agent's instructions (`system` or `developer`)."""
    role_of = role or role_field
    count = 0
    while count < len(messages) and role_of(messages[count]) in INSTRUCTION_ROLES:
        count += 1
    return count


def role_field(message: Any) -> Any:
    if isinstance(message, Mapping):
        return message.get("role")
    return getattr(message, "role", None)


def inject(context: Context, messages: Sequence[Any]) -> list[Any]:
    """Chat-shaped messages with the pack after the leading instructions and the turn block at the end."""
    result = list(messages)
    if context.system_block:
        result.insert(instruction_count(result), {"role": "system", "content": context.system_block})
    if context.turn_block:
        result.append({"role": "system", "content": context.turn_block})
    return result


def inject_prompt(prompt: Prompt, messages: Sequence[Any]) -> list[Any]:
    """Chat-shaped messages with the prompt's system slot after the leading instructions and its
    turn block at the end."""
    result = list(messages)
    if prompt.system:
        result.insert(instruction_count(result), {"role": "system", "content": prompt.system})
    if prompt.turn:
        result.append({"role": "system", "content": prompt.turn})
    return result


def join_instructions(*parts: str | None) -> str:
    """Instruction text followed by the Niadra blocks, for frameworks that take one system string."""
    return "\n\n".join(part for part in parts if part)


def record(session: AnySession | None, action: str, call: Callable[[AnySession], object]) -> None:
    """Runs one recording call (a turn, the end, a handoff) on the session and never raises."""
    if session is None:
        return
    try:
        call(session)
    except Exception as exc:
        warn(action, exc)


def customer_turn(session: AnySession | None, text: str | None, **event: Any) -> None:
    if text and text.strip():
        record(session, "record the customer's turn", lambda s: s.customer(text, **event))


def agent_turn(session: AnySession | None, text: str | None, *, usage: Any = None, **event: Any) -> None:
    if text and text.strip():
        record(session, "record the agent's turn", lambda s: s.agent(text, usage=usage, **event))


def handoff(
    session: AnySession | None, target: Literal["human", "agent"], reason: str | None = None, **options: Any
) -> None:
    """Records a transfer of the conversation; a task has no handoff, so it is skipped."""
    if not isinstance(session, (Conversation, AsyncConversation)):
        return
    try:
        session.handoff(target, reason=reason, **options)
    except Exception as exc:
        warn("record the handoff", exc)


def end(session: AnySession | None) -> None:
    record(session, "end the conversation", lambda s: s.end())


def prefetch(session: AnySession | None, text: str | None) -> None:
    """Sends a partial transcript of the customer's turn; never raises, never waits."""
    if session is None or not text or not text.strip():
        return
    try:
        session.prefetch(text)
    except Exception as exc:
        warn("prefetch the turn", exc)


def kit_of(session: AnySession | None) -> AnyKit | None:
    """The history kit bound to the session's customer; None without a session or a subject."""
    if session is None:
        return None
    try:
        return session.tools()
    except Exception as exc:
        warn("build the history tools", exc)
        return None


async def call_tool(kit: AnyKit, name: str, arguments: Any) -> str:
    """Runs one tool call from the model. Always a string for the model; never raises."""
    try:
        return await maybe_await(kit.call(name, arguments))
    except Exception as exc:
        warn(f"run {name}", exc)
        return _TOOL_FAILED


def call_tool_sync(kit: ToolKit, name: str, arguments: Any) -> str:
    try:
        return kit.call(name, arguments)
    except Exception as exc:
        warn(f"run {name}", exc)
        return _TOOL_FAILED


# STIR/SHAKEN: a full attestation (A) proves V2 for the call, a partial or gateway one (B, C) V1,
# as the API reads `voice.network_attestation`.
_ATTESTATION = {"A": Verification.V2, "B": Verification.V1, "C": Verification.V1}


def attestation_level(attestation: str | None) -> Verification | None:
    """The level a carrier's attestation proves: `A`, `B` or `C`, also as Twilio's `StirVerstat`.

    `TN-Validation-Passed-A` counts; a failed validation or no validation proves nothing.
    """
    if not attestation:
        return None
    value = attestation.strip().upper()
    if value.startswith("TN-VALIDATION-"):
        if not value.startswith("TN-VALIDATION-PASSED-"):
            return None
        value = value.rsplit("-", 1)[-1]
    return _ATTESTATION.get(value)


async def verify_attestation(session: AnySession | None, attestation: str | None) -> bool:
    """Calls `verify("network_attestation", ...)` when the carrier's attestation proves a level."""
    level = attestation_level(attestation)
    if session is None or level is None or session.subject is None:
        return False
    try:
        return await maybe_await(session.verify("network_attestation", level)) is not None
    except Exception as exc:
        warn("verify the call", exc)
        return False


def phone_or_none(number: str | None) -> Handle | None:
    """A phone handle from a caller id, or None when it is missing, withheld or not E.164."""
    if not number:
        return None
    value = number.strip()
    for prefix in ("tel:", "sip:", "whatsapp:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
    # Only an international number: a local format is ambiguous across countries, so no guess.
    value = value.split("@", 1)[0].split(";", 1)[0]
    try:
        return phone(value)
    except ValueError:
        return None


_PROVIDER_NOISE = re.compile(r"[^a-z0-9_.-]+")


def model_usage(
    provider: str | None,
    model: str | None,
    prompt_tokens: int | None,
    cached_tokens: int | None = 0,
    cache_write_tokens: int | None = 0,
) -> ModelUsage | None:
    """A `ModelUsage` from counts a framework reports, or None when they do not make one."""
    if not model or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        return None
    cached, written = max(0, cached_tokens or 0), max(0, cache_write_tokens or 0)
    name = _PROVIDER_NOISE.sub("-", (provider or provider_of(model)).strip().lower()).strip("-.")
    try:
        return ModelUsage(
            provider=name or provider_of(model),
            model=model,
            prompt_tokens=max(prompt_tokens, cached + written),
            cached_tokens=cached,
            cache_write_tokens=written,
        )
    except ValueError:
        return None


@dataclass(frozen=True)
class AgentMemoryOption:
    """The `agent_memory=` option of every adapter: `True`, or `{write, max_tokens, tags}`.

    The agent's notes go into the prompt before the customer's context, in the same system slot,
    and `search_agent_memory` joins the tools; `remember` joins them only with `write`, for a key
    that holds the `agent_memory:write` scope.
    """

    write: bool = False
    max_tokens: int = 300
    tags: tuple[str, ...] = ()


AgentMemoryLike = bool | Mapping[str, Any] | AgentMemoryOption | None


def memory_option(value: AgentMemoryLike) -> AgentMemoryOption | None:
    """The option as given to an adapter; None when the agent memory is off (the default)."""
    if value is None or value is False:
        return None
    if value is True:
        return AgentMemoryOption()
    if isinstance(value, AgentMemoryOption):
        return value
    return AgentMemoryOption(
        write=bool(value.get("write", False)),
        max_tokens=int(value.get("max_tokens", 300)),
        tags=tuple(value.get("tags") or ()),
    )


@dataclass(frozen=True)
class Prompt:
    """What an adapter puts into the prompt: the system slot after the instructions, and the end."""

    system: str = ""
    turn: str = ""
    context: Context | None = None
    agent_memory: str = ""


async def read_agent_memory(session: AnySession | None, option: AgentMemoryOption | None) -> str:
    """The agent's notes for the session's view, or "" when off, empty or failing."""
    if session is None or option is None:
        return ""
    try:
        block = await maybe_await(session.agent_memory(option.max_tokens, tags=list(option.tags) or None))
    except Exception as exc:
        warn("read the agent memory", exc)
        return ""
    return block.text if block.enabled else ""


async def read_prompt(
    session: AnySession | None, option: AgentMemoryOption | None = None, *, turn: str | None = None
) -> Prompt:
    """The agent's notes and the customer's context, placed for the prompt; empty on any failure."""
    notes = await read_agent_memory(session, option)
    context = await read_context(session, turn)
    system_block, turn_block = blocks(context)
    return Prompt(
        system=join_instructions(notes, system_block), turn=turn_block, context=context, agent_memory=notes
    )


def memory_kit_of(session: AnySession | None, option: AgentMemoryOption | None) -> AnyKit | None:
    """`kit_of()` with the agent memory tools the option asks for."""
    if option is None:
        return kit_of(session)
    if session is None:
        return None
    try:
        return session.tools(agent_memory=True, write_agent_memory=option.write)
    except Exception as exc:
        warn("build the history tools", exc)
        return None
