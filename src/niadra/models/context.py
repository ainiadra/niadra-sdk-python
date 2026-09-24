"""The read side: `POST /v1/context` and the history navigation kit."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import Field, StringConstraints, model_validator

from niadra.models._base import IdStr, Model, ResponseModel, ShortStr
from niadra.models.common import Handle, ObjectRef, SourceCoverage
from niadra.vocabulary import DeliveryPath, EventKind, Verification

VIEW_PATTERN = r"^(voice|chat|brief|full|custom|account|partner|task:[a-z0-9_]{1,40})$"
View = Annotated[str, StringConstraints(pattern=VIEW_PATTERN)]

# What navigation returns. A system event is never an item: it changes its object, so search `object`.
HistoryItemKind = Literal["episode", "fact", "open_item", "action", "object", "trait"]
HISTORY_ITEM_KINDS: tuple[str, ...] = get_args(HistoryItemKind)


class TargetModel(Model):
    """The LLM that will read the pack; lets the compiler aim at that model's cache floor."""

    provider: ShortStr
    model: ShortStr

    @classmethod
    def parse(cls, value: str) -> TargetModel:
        """Parses `provider/model`, e.g. `openai/gpt-4.1`. Everything after the first slash is the model."""
        provider, _, model = value.partition("/")
        if not provider or not model:
            raise ValueError("a target looks like `provider/model`")
        return cls(provider=provider, model=model)


class ContextRequest(Model):
    subject: Handle | None = None
    object: ObjectRef | None = None
    about: Handle | None = Field(default=None, description="The account or partner the person acts for.")
    view: View = "chat"
    verification: Verification = Verification.V0
    conversation_id: IdStr | None = None
    task_id: IdStr | None = None
    query: Annotated[str, StringConstraints(max_length=2000)] | None = None
    delta: bool = False
    target: TargetModel | None = None
    known_etag: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> ContextRequest:
        if (self.subject is None) == (self.object is None):
            raise ValueError("pass exactly one of `subject` or `object`")
        if self.conversation_id and self.task_id:
            raise ValueError("pass `conversation_id` or `task_id`, not both")
        return self


class VerificationResult(ResponseModel):
    requested: Verification
    effective: Verification
    reason: str | None = Field(
        default=None, description="Why effective is lower: `source_ceiling`, `not_proven`."
    )


class LiveTurn(ResponseModel):
    """A recent turn from another channel that the pinned pack has not absorbed yet."""

    at: datetime
    channel: str
    kind: EventKind | str
    speaker: str
    text: str
    source_id: str


class CacheDirectives(ResponseModel):
    """How the target model can cache the pack.

    `breakpoints` are character offsets into `text` where a provider with explicit cache
    control may place a breakpoint (at most two are used by the pack). `cacheable` is a
    decision, not a default: one-turn conversations pay for cache writes they never read.
    """

    breakpoints: list[int] = Field(default_factory=list)
    ttl_seconds: int | None = None
    floor_tokens: int | None = None
    cacheable: bool = False
    salt: str = ""


class ContextResponse(ResponseModel):
    not_modified: bool = False
    text: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)
    version: str
    etag: str
    manifest_hash: str | None = None
    as_of: datetime | None = None
    lag_seconds: float | None = None
    coverage: list[SourceCoverage] = Field(default_factory=list)
    verification: VerificationResult
    withheld: int = 0
    live: list[LiveTurn] = Field(default_factory=list)
    live_complete: bool = True
    delta: str | None = None
    cache: CacheDirectives | None = None
    timing: dict[str, float] = Field(default_factory=dict)
    path: DeliveryPath | str
    degraded: bool = False


class HistoryFilters(Model):
    since: datetime | None = None
    until: datetime | None = None
    channels: list[ShortStr] = Field(default_factory=list)
    categories: list[ShortStr] = Field(default_factory=list)
    item_kinds: list[HistoryItemKind] = Field(default_factory=list)
    outcome: ShortStr | None = None
    object: ObjectRef | None = None


class SearchRequest(Model):
    subject: Handle
    about: Handle | None = None
    query: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    filters: HistoryFilters = Field(default_factory=HistoryFilters)
    max_tokens: int = Field(default=800, ge=50, le=4000)
    verification: Verification = Verification.V0
    conversation_id: IdStr | None = None
    task_id: IdStr | None = None


class HistoryItem(ResponseModel):
    id: str
    kind: str
    text: str
    at: datetime
    channel: str | None = None
    source_id: str | None = None
    outcome: str | None = None
    confidence: float | None = None
    origin_event_id: str | None = None


class Recurrence(ResponseModel):
    category: str
    occurrences: int
    window_days: int
    last_at: datetime | None = None
    last_outcome: str | None = None
    last_resolution: str | None = None


class SearchResponse(ResponseModel):
    items: list[HistoryItem] = Field(default_factory=list)
    recurrence: Recurrence | None = None
    withheld: int = 0
    as_of: datetime | None = None
    tokens_used: int = 0
    degraded: str | None = Field(default=None, description="`text_only` when the encoder was unavailable.")


class TimelineRequest(Model):
    subject: Handle
    about: Handle | None = None
    filters: HistoryFilters = Field(default_factory=HistoryFilters)
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    verification: Verification = Verification.V0
    conversation_id: IdStr | None = None


class TimelineResponse(ResponseModel):
    items: list[HistoryItem] = Field(default_factory=list)
    next_cursor: str | None = None
    withheld: int = 0
    as_of: datetime | None = None


class Promise(ResponseModel):
    by: Literal["company", "customer"]
    what: str
    due_at: datetime | None = None
    status: str


class OpenedItem(ResponseModel):
    id: str
    kind: Literal["episode", "object"]
    summary: str
    requested: str | None = None
    promises: list[Promise] = Field(default_factory=list)
    outcome: str | None = None
    resolution: str | None = None
    derived: list[HistoryItem] = Field(default_factory=list)
    timeline: list[HistoryItem] = Field(default_factory=list)
    excerpt: str | None = Field(
        default=None, description="Literal transcript excerpt; needs an elevated scope."
    )
    as_of: datetime | None = None


class ObjectState(ResponseModel):
    ref: ObjectRef
    state: dict[str, Any]
    as_of: datetime
    source_id: str
    record_ref: str | None = None
    open_items: list[HistoryItem] = Field(default_factory=list)


class ToolDefinition(ResponseModel):
    """A function-calling tool definition in the most common JSON Schema shape."""

    type: Literal["function"] = "function"
    function: dict[str, Any]
