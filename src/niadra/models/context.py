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
    # Sent only when asked for, so a cell that does not know the field yet still takes the request.
    format: Literal["text", "json"] | None = Field(
        default=None, description="`json` also returns `pack`: the pack as typed sections."
    )
    # Sent only when asked for, so a cell that does not know the field yet still takes the request.
    explain: bool | None = Field(
        default=None,
        description='Memory v2, with `format: "json"` and `query`: each of `pack.slots` also says `why` '
        "it was chosen (its position in each retrieval channel, each channel's weighted share of the fused "
        "score, the weights version, the rule of a derived line). It changes nothing else: the pinned text, "
        "the slots chosen and the receipt are the same bytes with or without it.",
    )

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


PackSectionName = Literal[
    "account",
    "customer",
    "facts",
    "stable_patterns",
    "meta",
    "history",
    "summary",
    "episodes",
    "volatile_patterns",
    "actions",
    "objects",
    "pending",
]


class PackSection(ResponseModel):
    """One line group of the pack. Read `name`, stable across languages; `label` is for people."""

    name: PackSectionName | str
    label: str
    layer: Literal["account", "stable", "volatile"] | str
    lines: list[str] = Field(default_factory=list)


class PackStamp(ResponseModel):
    etag: str
    version: str
    as_of: datetime | None = None
    manifest_hash: str | None = None


PackSlotDerived = Literal["count", "no_record", "withheld"]


class SlotChannelRank(ResponseModel):
    """One retrieval channel's part in a slot's fused score (weighted reciprocal rank fusion)."""

    channel: str = Field(description="`exact`, `values`, `lexical`, `temporal`, `semantic` or `linked`.")
    position: int = Field(description="1-based, in that channel's own ranking for the turn.")
    weight: float = Field(description="The channel's weight in this fusion.")
    contribution: float = Field(description="`weight / (60 + position)`: what the channel added to `score`.")


class SlotWhy(ResponseModel):
    """Why a line took a slot (`explain`). Ids, positions and numbers; never a line or a value."""

    item_id: str | None = Field(
        default=None, description="The item, as manifests and context-use name it; absent for a derived line."
    )
    score: float | None = Field(
        default=None, description="The fused score: the sum of the channels' contributions."
    )
    channels: list[SlotChannelRank] = Field(
        default_factory=list, description="Each channel that ranked the item, in fusion order."
    )
    weights_version: int | None = Field(
        default=None, description="The space's learned fusion weights used; absent for the defaults."
    )
    via: str | None = Field(
        default=None,
        description="For an item the `linked` channel brought: the exact match it is tied to, as `item_id`.",
    )
    excerpt: bool = Field(
        default=False, description="The line was cut to the sentences that answer the turn."
    )
    rule: str | None = Field(
        default=None,
        description="For a derived line: `count_complaints`, `count_conversations`, `no_record` or "
        "`withheld_may_hold`.",
    )
    basis: dict[str, Any] = Field(
        default_factory=dict,
        description="For a derived line, what the rule counted or missed: `basis` (the conversation that "
        "chose the category), `category`, `counted`, `window_days`; `asked_types`, `identifiers` (how many "
        "numbers the turn named, never which), `withheld`.",
    )


class PackSlot(ResponseModel):
    """One line of this turn's slots (memory v2): an item the customer's last turn selected, or a
    line derived from memory. The same line as in `ContextResponse.slots`.

    `section` names the pack section the item comes from (`episodes`, `objects`...), is `guard`
    for a guard line (a value the agent must not state otherwise, typed in `ContextResponse.guards`),
    or is `derived` for a line the server derived: `derived` then says which (`count`, how many times a
    topic came back, with the dates; `no_record`, that memory holds nothing about what was asked;
    `withheld`, that items held back until verification may hold it). `channels` are the ways the
    item was found: `exact`, `lexical`, `temporal`, `values`, `semantic`, `linked`.
    """

    section: PackSectionName | str
    id: str | None = Field(
        default=None,
        description="The short id of the item the line states (the last eight hex digits of its public "
        "id); a guard line's names its guard in `Backing.guard_violations`. None on a derived line.",
    )
    derived: PackSlotDerived | str | None = None
    channels: list[str] = Field(default_factory=list)
    text: str
    why: SlotWhy | None = Field(default=None, description="With `explain`: why this line was chosen.")


class PackGuard(ResponseModel):
    """What one guard line states (memory v2): the value memory holds for a kind the customer's turn
    asked about, by the precedence of who stated it (the system of record, then a human agent). The
    agent must not state another; `Conversation.agent()` checks its answer against it."""

    id: str = Field(description="The value's short id, as the guard line's `PackSlot.id`.")
    value_type: str = Field(
        description="`protocol`, `ticket`, `order`, `record`, `receipt`, `postal_code`, `amount`, `date` "
        "or `code`."
    )
    value: str = Field(description="The value as the line writes it.")


class ContextPack(ResponseModel):
    """The pack as data (`format="json"`), in the `context-pack.v1` shape, for programs that build
    their own prompt. The same content as `text`, plus this turn's `slots`, which are never part of
    `text`. A server that answers `context-pack.v0` sends no `slots`; the list is then empty."""

    spec: str = "context-pack.v1"
    view: str
    verification: Verification
    withheld: int
    as_of: datetime | None = None
    preamble: str
    sections: list[PackSection] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    stamp: PackStamp
    slots: list[PackSlot] = Field(default_factory=list)


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
    slots: str | None = Field(
        default=None,
        description="Memory v2, on a read with `query`: what the customer's last turn selected from memory "
        "for this turn, a tagged block for the end of the prompt. Never part of `text`. Servers without "
        "memory v2 do not send it.",
    )
    guards: list[PackGuard] = Field(
        default_factory=list,
        description="Memory v2: what the guard lines among `slots` state, typed. `Conversation.agent()` "
        "checks the answer against them and names a guard it went against on the turn.",
    )
    cache: CacheDirectives | None = None
    timing: dict[str, float] = Field(default_factory=dict)
    path: DeliveryPath | str
    degraded: bool = False
    pack: ContextPack | None = None


class PrefetchRequest(Model):
    """Body of `POST /v1/context/prefetch`: a partial transcript of the customer's turn, sent while they
    are still speaking, so the server warms what the final read will need. It answers nothing."""

    subject: Handle | None = None
    object: ObjectRef | None = None
    about: Handle | None = None
    view: View = "voice"
    verification: Verification = Verification.V0
    conversation_id: IdStr | None = None
    task_id: IdStr | None = None
    query: Annotated[str, StringConstraints(min_length=1, max_length=2000)]

    @model_validator(mode="after")
    def _one_target(self) -> PrefetchRequest:
        if (self.subject is None) == (self.object is None):
            raise ValueError("pass exactly one of `subject` or `object`")
        if self.conversation_id and self.task_id:
            raise ValueError("pass `conversation_id` or `task_id`, not both")
        return self


class HistoryFilters(Model):
    since: datetime | None = None
    until: datetime | None = None
    when: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = Field(
        default=None,
        description="A time phrase in the customer's words (`last week`, `semana passada`, `en marzo`), in "
        "Portuguese, English or Spanish. It narrows `since` and `until`; one the server cannot read comes "
        "back in `ignored`.",
    )
    channels: list[ShortStr] = Field(default_factory=list)
    categories: list[ShortStr] = Field(default_factory=list)
    item_kinds: list[HistoryItemKind] = Field(default_factory=list)
    outcome: ShortStr | None = None
    object: ObjectRef | None = None
    show_expired: bool | None = Field(default=None, description="Also items whose `valid_until` has passed.")
    where: dict[str, Any] | None = Field(
        default=None,
        description="Conditions joined by `AND`, `OR` and `NOT` over the row fields `id`, `kind`, `channel`, "
        "`category`, `outcome`, `source_id`, `vendor`, `at`, `valid_until`, `confidence`, `text`, "
        "`object_type` and `object_namespace`, with `eq`, `ne`, `in`, `nin`, `gt`, `gte`, `lt`, `lte`, "
        "`contains`, `icontains` and `exists`. A bare value means `eq`, a list means `in`. It narrows what "
        "the policy already let through and never changes the order.",
    )


class SearchRequest(Model):
    subject: Handle
    about: Handle | None = None
    query: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    filters: HistoryFilters = Field(default_factory=HistoryFilters)
    max_tokens: int = Field(default=800, ge=50, le=4000)
    verification: Verification = Verification.V0
    conversation_id: IdStr | None = None
    task_id: IdStr | None = None
    limit: int | None = Field(default=None, ge=1, le=100, description="At most this many items.")


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
    valid_until: datetime | None = None


class TimeWindow(ResponseModel):
    """The period a search or timeline covered, after reading `since`, `until` and `when`."""

    since: datetime | None = None
    until: datetime | None = None


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
    window: TimeWindow | None = None
    ignored: list[str] = Field(default_factory=list, description="Filters the server could not read.")


class TimelineRequest(Model):
    subject: Handle
    about: Handle | None = None
    filters: HistoryFilters = Field(default_factory=HistoryFilters)
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    verification: Verification = Verification.V0
    conversation_id: IdStr | None = None


class OpenItemRequest(Model):
    """Body of `POST /v1/history/open`: the conversation id may be a phone number or an e-mail, and the
    customer is personal data, so neither goes in a URL."""

    item_id: IdStr
    subject: Handle | None = Field(
        default=None, description="The customer the item must belong to; any other item answers 404."
    )
    verification: Verification = Verification.V0
    conversation_id: IdStr | None = None


class TimelineResponse(ResponseModel):
    items: list[HistoryItem] = Field(default_factory=list)
    next_cursor: str | None = None
    withheld: int = 0
    as_of: datetime | None = None
    window: TimeWindow | None = None
    ignored: list[str] = Field(default_factory=list, description="Filters the server could not read.")


class ItemVersion(ResponseModel):
    """One earlier state of an opened item: when it changed and what changed."""

    version: int
    changed_at: datetime
    what_changed: str


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
        default=None, description="The server no longer sends a transcript excerpt; kept for code reading it."
    )
    as_of: datetime | None = None
    versions: list[ItemVersion] = Field(default_factory=list)


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
