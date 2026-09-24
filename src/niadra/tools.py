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
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from niadra._base import HandleLike, VerificationLike, as_handle
from niadra.models.context import HISTORY_ITEM_KINDS, HistoryFilters
from niadra.vocabulary import Verification

if TYPE_CHECKING:
    from niadra._async_client import AsyncNiadra
    from niadra._client import Niadra

SEARCH = "search_customer_history"
TIMELINE = "get_customer_timeline"
OPEN = "open_history_item"

_PERIOD: dict[str, Any] = {
    "since": {
        "type": "string",
        "format": "date-time",
        "description": "Only items at or after this ISO 8601 time.",
    },
    "until": {
        "type": "string",
        "format": "date-time",
        "description": "Only items before this ISO 8601 time.",
    },
    "channels": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Only these channels, such as whatsapp, voice, email.",
    },
    "item_kinds": {
        "type": "array",
        "items": {"type": "string", "enum": list(HISTORY_ITEM_KINDS)},
        "description": "Only these kinds of history items.",
    },
}

# Word for word the same definitions as the TypeScript SDK: a model sees one toolset, whatever
# language the agent is written in.
BUILTIN_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": SEARCH,
            "description": (
                "Search this customer's past conversations, actions and business objects by meaning and "
                "keywords. Use it when the customer refers to something that happened before and the "
                "details are not in the customer context you already have. Do not use it for facts already "
                "listed there. The result also says how often the same kind of issue came back. To read one"
                " result in full, call open_history_item."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for, in the customer's own terms.",
                    },
                    **_PERIOD,
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only these topics.",
                    },
                    "outcome": {
                        "type": "string",
                        "description": "Only items with this outcome, such as resolved.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TIMELINE,
            "description": (
                "List this customer's history in chronological order, one line per item. Use it when you "
                "need the sequence of events, for example what happened since a given date. Prefer "
                "search_customer_history when you are looking for something specific."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_PERIOD,
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "How many items. Defaults to 20.",
                    },
                    "cursor": {"type": "string", "description": "The next_cursor of a previous page."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": OPEN,
            "description": (
                "Open one history item returned by search_customer_history or get_customer_timeline: its "
                "summary, what was requested, commitments made by either side, the outcome and the "
                "resolution. Only use ids returned by those tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "The item id."}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
]

_UNAVAILABLE = "history unavailable right now; answer from the context you already have"
_VOICE_MAX_TOKENS = 300
_TEXT_MAX_TOKENS = 800

Arguments = str | Mapping[str, Any] | None


@dataclass(frozen=True)
class _Plan:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def _render(result: BaseModel | None) -> str:
    if result is None or getattr(result, "error", None) is not None:
        return _error(_UNAVAILABLE)
    payload = result.model_dump(mode="json", exclude_none=True, exclude={"error"})
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
            max_tokens = _VOICE_MAX_TOKENS if self.voice else _TEXT_MAX_TOKENS
            return _Plan(
                "search",
                (self.subject, query),
                {
                    **common,
                    "about": self.about,
                    "filters": filters,
                    "max_tokens": max_tokens,
                    "task_id": self.task_id,
                },
            )
        if name == TIMELINE:
            try:
                limit = min(100, max(1, int(args.get("limit") or 20)))
            except (TypeError, ValueError):
                return _error("invalid arguments")
            cursor = args.get("cursor")
            return _Plan(
                "timeline",
                (self.subject,),
                {**common, "about": self.about, "filters": filters, "cursor": cursor, "limit": limit},
            )
        if name == OPEN:
            item_id = str(args.get("id") or "").strip()
            if not item_id:
                return _error("id is required")
            return _Plan("open", (item_id,), {**common, "task_id": self.task_id})
        return _error(f"unknown tool {name}")


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
    keys = ("since", "until", "channels", "categories", "item_kinds", "outcome")
    return HistoryFilters.model_validate({k: arguments[k] for k in keys if arguments.get(k) is not None})


class ToolKit(_ToolKitBase):
    def __init__(self, client: Niadra, definitions: list[dict[str, Any]], **binding: Any) -> None:
        super().__init__(definitions, **binding)
        self._client = client

    def call(self, name: str, arguments: Arguments = None) -> str:
        """Runs one tool call from the model and returns the text to send back as the tool result."""
        plan = self._plan(name, arguments)
        if isinstance(plan, str):
            return plan
        return _render(getattr(self._client, plan.method)(*plan.args, **plan.kwargs))


class AsyncToolKit(_ToolKitBase):
    def __init__(self, client: AsyncNiadra, definitions: list[dict[str, Any]], **binding: Any) -> None:
        super().__init__(definitions, **binding)
        self._client = client

    async def call(self, name: str, arguments: Arguments = None) -> str:
        """Runs one tool call from the model and returns the text to send back as the tool result."""
        plan = self._plan(name, arguments)
        if isinstance(plan, str):
            return plan
        return _render(await getattr(self._client, plan.method)(*plan.args, **plan.kwargs))
