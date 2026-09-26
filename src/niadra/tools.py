"""The history navigation kit as function-calling tools, bound to one customer.

The definitions never mention the customer. The kit carries the subject itself and adds it
to every call, so a model can choose what to look for but never whose history it reads.

```python
kit = niadra.tools(phone("+5511912345678"), conversation_id=thread_id)
response = openai.chat.completions.create(model=..., messages=..., tools=kit.definitions)
for call in response.choices[0].message.tool_calls or []:
    output = kit.call(call.function.name, call.function.arguments)
```

`call()` always returns a string for the model: compact JSON with the results, or a short
error that tells the model to carry on with the context it already has.

The definitions are the API's own, word for word (`GET /v1/history/tools?agent_memory=true`),
shipped in `tool_definitions.json`: the same five tools, in the same order and the same JSON,
in every Niadra SDK and in the MCP server. With `agent_memory=True` the kit adds
`search_agent_memory` (the agent's own working notes), and `remember` only with
`write_agent_memory=True`, for a key that holds the `agent_memory:write` scope.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from niadra._base import HandleLike, VerificationLike, as_handle
from niadra.errors import APIError
from niadra.models.context import HistoryFilters
from niadra.vocabulary import Verification

if TYPE_CHECKING:
    from niadra._async_client import AsyncNiadra
    from niadra._client import Niadra

SEARCH = "search_customer_history"
TIMELINE = "get_customer_timeline"
OPEN = "open_history_item"
SEARCH_AGENT_MEMORY = "search_agent_memory"
REMEMBER = "remember"

ALL_DEFINITIONS: list[dict[str, Any]] = json.loads(
    files("niadra").joinpath("tool_definitions.json").read_text(encoding="utf-8")
)
"""The five tools, as the API defines them: the three of the history, then the agent's memory."""

# The history kit every SDK hands to a model: search, timeline and open.
BUILTIN_DEFINITIONS: list[dict[str, Any]] = ALL_DEFINITIONS[:3]
# The agent's own working notes: `search_agent_memory`, then `remember`.
AGENT_MEMORY_DEFINITIONS: list[dict[str, Any]] = ALL_DEFINITIONS[3:]


def definitions(*, agent_memory: bool = False, write_agent_memory: bool = False) -> list[dict[str, Any]]:
    """The kit's definitions: the history, plus the agent's memory when asked for.

    `remember` comes only with `write_agent_memory`: a tool the key cannot use is a wasted call.
    """
    chosen = list(BUILTIN_DEFINITIONS)
    if agent_memory:
        for definition in AGENT_MEMORY_DEFINITIONS:
            if write_agent_memory or definition["function"]["name"] != REMEMBER:
                chosen.append(definition)
    return chosen


_UNAVAILABLE = "history unavailable right now; answer from the context you already have"
MEMORY_UNAVAILABLE = json.dumps(
    {"error": "unavailable", "detail": "agent memory is unavailable right now"}, separators=(",", ":")
)
"""What the agent memory tools tell the model on any failure but a refused note."""
PERSONAL_DATA = json.dumps(
    {
        "error": "personal_data",
        "detail": "The note has personal data. Rewrite it so it helps with any customer, without names, "
        "phones, e-mails, documents or ids.",
    },
    separators=(",", ":"),
)
"""What `remember` tells the model when the API refused a note with personal data (422)."""

_PERSONAL_DATA_CODE = "personal_data_in_agent_memory"
_VOICE_MAX_TOKENS = 300
_TEXT_MAX_TOKENS = 800
_FILTER_KEYS = ("since", "until", "when", "channels", "categories", "item_kinds", "outcome")

Arguments = str | Mapping[str, Any] | None


@dataclass(frozen=True)
class _Plan:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    render: Callable[[Any], str]


def _render(result: BaseModel | None) -> str:
    if result is None or getattr(result, "error", None) is not None:
        return _error(_UNAVAILABLE)
    payload = result.model_dump(mode="json", exclude_none=True, exclude={"error"})
    return _compact(payload)


def _render_notes(result: Any) -> str:
    if result is None:
        return MEMORY_UNAVAILABLE
    notes = [
        note.model_dump(mode="json", include={"note_id", "kind", "title", "body", "tags"}, exclude_none=True)
        for note in result
    ]
    return _compact({"notes": notes})


def _render_remembered(result: Any) -> str:
    error = getattr(result, "error", None)
    if error == _PERSONAL_DATA_CODE:
        return PERSONAL_DATA
    if result is None or error is not None:
        return MEMORY_UNAVAILABLE
    note = getattr(result, "note", None)
    if note is not None:
        return _compact({"saved": True, "note_id": note.note_id, "version": note.version})
    return _compact({"saved": False, "proposal_id": result.proposal_id, "status": "waiting for review"})


def _compact(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _error(message: str) -> str:
    return json.dumps({"error": message})


class _ToolKitBase:
    def __init__(
        self,
        definitions: list[dict[str, Any]],
        *,
        subject: HandleLike,
        about: HandleLike | None,
        conversation_id: str | None,
        task_id: str | None,
        verification: VerificationLike | Callable[[], Verification],
        voice: bool,
    ) -> None:
        self.definitions = definitions
        self.subject = as_handle(subject)
        self.about = as_handle(about) if about is not None else None
        self.conversation_id = conversation_id
        self.task_id = task_id
        self.voice = voice
        if callable(verification):
            self._level = verification
        else:
            fixed = Verification(verification)
            self._level = lambda: fixed

    @property
    def verification(self) -> Verification:
        """The level the calls are made at. A session's kit reads the session's current level."""
        return self._level()

    @property
    def names(self) -> list[str]:
        return [str(d["function"]["name"]) for d in self.definitions]

    def anthropic_definitions(self) -> list[dict[str, Any]]:
        """The same tools in the Anthropic Messages API shape: `name`, `description`, `input_schema`."""
        return [
            {
                "name": d["function"]["name"],
                "description": d["function"].get("description", ""),
                "input_schema": d["function"].get("parameters", {"type": "object", "properties": {}}),
            }
            for d in self.definitions
        ]

    def _plan(self, name: str, arguments: Arguments) -> _Plan | str:
        """Turns a model's tool call into a client call with the bound customer, or an error for the model."""
        if name not in self.names:
            return _error(f"unknown tool {name}")
        try:
            args = _parse_arguments(arguments)
            filters = _filters(args)
        except (ValueError, ValidationError):
            return _error("invalid arguments")
        common = {
            "verification": self.verification,
            "conversation_id": self.conversation_id,
            "voice": self.voice,
        }
        if name == SEARCH:
            query = str(args.get("query") or "").strip()
            if not query:
                return _error("query is required")
            try:
                max_tokens = _max_tokens(args.get("max_tokens"), self.voice)
            except (TypeError, ValueError):
                return _error("invalid arguments")
            kwargs = {**common, "about": self.about, "filters": filters, "max_tokens": max_tokens}
            return _Plan("search", (self.subject, query), {**kwargs, "task_id": self.task_id}, _render)
        if name == TIMELINE:
            try:
                limit = min(100, max(1, int(args.get("limit") or 20)))
            except (TypeError, ValueError):
                return _error("invalid arguments")
            kwargs = {**common, "about": self.about, "filters": filters, "cursor": args.get("cursor")}
            return _Plan("timeline", (self.subject,), {**kwargs, "limit": limit}, _render)
        if name == OPEN:
            item_id = str(args.get("id") or "").strip()
            if not item_id:
                return _error("id is required")
            # The bound customer goes along, so the server opens only an item of theirs.
            return _Plan("open", (item_id,), {**common, "subject": self.subject}, _render)
        return self._agent_memory_plan(name, args)

    def _agent_memory_plan(self, name: str, args: dict[str, Any]) -> _Plan | str:
        tags = args.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return _error("invalid arguments")
        where = {"conversation_id": self.conversation_id, "task_id": self.task_id}
        if name == SEARCH_AGENT_MEMORY:
            query = str(args.get("query") or "").strip()
            if not query:
                return _error("query is required")
            return _Plan("search_agent_memory", (query,), {"tags": tags, **where}, _render_notes)
        kind, title, body = (str(args.get(k) or "").strip() for k in ("kind", "title", "body"))
        if not (kind and title and body):
            return _error("kind, title and body are required")
        evidence = {k: v for k, v in where.items() if v}
        return _Plan(
            "remember",
            (kind, title, body),
            {"tags": tags, "evidence": evidence or None},
            _render_remembered,
        )


def _parse_arguments(arguments: Arguments) -> dict[str, Any]:
    if arguments is None or arguments == "":
        return {}
    if isinstance(arguments, str):
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must be a JSON object")
        return parsed
    return dict(arguments)


def _filters(arguments: dict[str, Any]) -> HistoryFilters:
    """The model's `filters` object, over the flat fields of 0.1 kits, which are still read."""
    nested = arguments.get("filters")
    if nested is not None and not isinstance(nested, Mapping):
        raise ValueError("filters must be an object")
    merged = {k: arguments[k] for k in _FILTER_KEYS if arguments.get(k) is not None}
    merged.update({k: v for k, v in (nested or {}).items() if k in _FILTER_KEYS and v is not None})
    if isinstance(merged.get("when"), str):
        merged["when"] = merged["when"][:100]
    kinds = merged.get("item_kinds")
    if isinstance(kinds, list):
        # A system event is never an item: it changes its object, so 0.1.1 kits asked for it by that name.
        merged["item_kinds"] = list(dict.fromkeys("object" if k == "system_event" else k for k in kinds))
    return HistoryFilters.model_validate(merged)


def _max_tokens(value: Any, voice: bool) -> int:
    default = _VOICE_MAX_TOKENS if voice else _TEXT_MAX_TOKENS
    if value is None:
        return default
    return min(4000, max(50, int(value)))


_MEMORY_METHODS = frozenset({"search_agent_memory", "remember"})


def _failed(plan: _Plan, exc: Exception) -> str:
    """A memory tool's failure as the model's answer; the history tools' failures are the caller's."""
    if isinstance(exc, APIError) and exc.code == _PERSONAL_DATA_CODE:
        return PERSONAL_DATA
    if plan.method in _MEMORY_METHODS:
        return MEMORY_UNAVAILABLE
    raise exc


def _remembered(plan: _Plan, call: Callable[[], Any]) -> str:
    try:
        return plan.render(call())
    except Exception as exc:
        return _failed(plan, exc)


class ToolKit(_ToolKitBase):
    def __init__(
        self,
        client: Niadra,
        definitions: list[dict[str, Any]],
        *,
        observe: Callable[[str], None] | None = None,
        **binding: Any,
    ) -> None:
        """`observe` receives each tool result: a conversation's kit records them as sources of the
        agent's answers (`niadra.backing`)."""
        super().__init__(definitions, **binding)
        self._client = client
        self._observe = observe

    def call(self, name: str, arguments: Arguments = None) -> str:
        """Runs one tool call from the model and returns the text to send back as the tool result."""
        plan = self._plan(name, arguments)
        if isinstance(plan, str):
            return plan
        method = getattr(self._client, plan.method)
        return _observed(self._observe, _remembered(plan, lambda: method(*plan.args, **plan.kwargs)))


class AsyncToolKit(_ToolKitBase):
    def __init__(
        self,
        client: AsyncNiadra,
        definitions: list[dict[str, Any]],
        *,
        observe: Callable[[str], None] | None = None,
        **binding: Any,
    ) -> None:
        super().__init__(definitions, **binding)
        self._client = client
        self._observe = observe

    async def call(self, name: str, arguments: Arguments = None) -> str:
        """Runs one tool call from the model and returns the text to send back as the tool result."""
        plan = self._plan(name, arguments)
        if isinstance(plan, str):
            return plan
        try:
            result = await getattr(self._client, plan.method)(*plan.args, **plan.kwargs)
        except Exception as exc:
            return _failed(plan, exc)
        return _observed(self._observe, plan.render(result))


def _observed(observe: Callable[[str], None] | None, text: str) -> str:
    if observe is not None:
        with suppress(Exception):  # recording a source never fails the tool call
            observe(text)
    return text
