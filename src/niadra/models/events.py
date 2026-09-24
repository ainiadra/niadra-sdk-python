"""The write side: items of `POST /v1/batch`.

A batch mixes items of several `type`s. One bad item never fails the batch: the server
answers 207 with one error per rejected item.

Unlike the server models, `idempotency_key` and `occurred_at` have defaults here: the SDK
mints a UUIDv7 and stamps the current time when the caller does not supply them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from niadra._ids import new_key
from niadra.models._base import IdStr, Model, ResponseModel, ShortStr
from niadra.models.common import Handle, ObjectRef, Subject
from niadra.usage import usage_fields
from niadra.vocabulary import AssertionMethod, EventKind, Speaker, SubjectKind, Verification, Visibility

MAX_EVENT_TEXT = 200_000
MAX_BATCH_ITEMS = 500
MAX_MEDIA_BYTES = 500 * 1024 * 1024

VerifyMethod = Literal["otp_whatsapp", "otp_sms", "login", "kba", "network_attestation", "human_agent"]
FeedbackAction = Literal["retract_fact", "correct_fact", "resolve_open_item", "conversation_outcome"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SpeakerRef(Model):
    role: Speaker
    id: ShortStr | None = Field(default=None, description="Agent or attendant id inside the source.")


class Content(Model):
    type: Literal["text", "audio", "image", "file"] = "text"
    text: Annotated[str, StringConstraints(max_length=MAX_EVENT_TEXT)] | None = None
    media_ref: IdStr | None = Field(default=None, description="Reference returned by /v1/media/uploads.")
    media_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    transcript: Annotated[str, StringConstraints(max_length=MAX_EVENT_TEXT)] | None = None
    stt_confidence: float | None = Field(default=None, ge=0, le=1)


class VoiceInfo(Model):
    ani: ShortStr | None = None
    dnis: ShortStr | None = None
    trunk: ShortStr | None = None
    network_attestation: Literal["A", "B", "C"] | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: ShortStr | None = None
    recording_ref: IdStr | None = None
    turn_offset_ms: int | None = Field(default=None, ge=0)


class Closes(Model):
    """The open item an action fulfils: by `item_id`, or by `object` and canonical `operation`."""

    item_id: str | None = None
    object: ObjectRef | None = None
    operation: ShortStr | None = None

    @field_validator("object", mode="before")
    @classmethod
    def _shorthand(cls, value: Any) -> Any:
        return ObjectRef.parse(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _one_way(self) -> Closes:
        if bool(self.item_id) == bool(self.object and self.operation):
            raise ValueError("closes takes either item_id, or object and operation")
        return self


class ActionInfo(Model):
    operation: ShortStr = Field(description="Canonical operation, e.g. `credit`, `reschedule`.")
    result: Annotated[str, StringConstraints(max_length=2000)] | None = None
    purpose: ShortStr | None = None
    closes: Closes | None = None
    corrects_action_id: str | None = None


class ContextStamp(Model):
    """Which context the agent's prompt carried and when it went in.

    Set on the agent's own turns and actions, so measurement can tell a context that arrived
    after the agent spoke from one that was in the prompt and went unused.
    """

    etag: IdStr | None = None
    injected_at: datetime


class ModelUsage(Model):
    """What the model provider reported for the call behind an agent's turn.

    `wrap()` reads it from every call it sees; without `wrap()`, pass it with the turn:
    `conversation.agent(text, usage=response)` takes the provider's response (OpenAI or
    Anthropic) or a `ModelUsage`. Niadra sums it per agent, vendor and model and shows the
    prompt cache's hit rate and estimated savings in the Console.
    """

    provider: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")] = Field(
        description="Who served the call, lowercase: `openai`, `anthropic`, a router or a cloud."
    )
    model: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}$")] = Field(
        description="The model the provider says answered, e.g. `gpt-4.1-2025-04-14`."
    )
    prompt_tokens: int = Field(ge=0, le=100_000_000, description="Every input token, cached ones included.")
    cached_tokens: int = Field(default=0, ge=0, description="Input tokens read from the provider's cache.")
    cache_write_tokens: int = Field(
        default=0, ge=0, description="Input tokens written to the cache (Anthropic's cache creation)."
    )

    @model_validator(mode="after")
    def _parts_fit(self) -> ModelUsage:
        if self.cached_tokens + self.cache_write_tokens > self.prompt_tokens:
            raise ValueError("cached and written tokens are part of prompt_tokens")
        return self

    @classmethod
    def from_response(
        cls, response: Any, *, provider: str | None = None, model: str | None = None
    ) -> ModelUsage | None:
        """Reads an OpenAI or Anthropic response, or its bare `usage` with `model=`. None without usage."""
        fields = usage_fields(response, provider=provider, model=model)
        if fields is None:
            return None
        try:
            return cls(**fields)
        except ValueError:
            return None


class EventItem(Model):
    """A message, a system event or an agent action."""

    type: Literal["event"] = "event"
    kind: EventKind = EventKind.MESSAGE
    idempotency_key: IdStr = Field(
        default_factory=new_key, description="Provider message id, or a UUIDv7 minted by the SDK."
    )
    channel: ShortStr
    conversation_id: IdStr | None = None
    conversation_aliases: list[IdStr] = Field(default_factory=list, max_length=8)
    task_id: IdStr | None = None
    handles: list[Handle] = Field(default_factory=list, max_length=16)
    subjects: list[Subject] = Field(default_factory=list, max_length=8)
    object_refs: list[ObjectRef] = Field(default_factory=list, max_length=16)
    speaker: SpeakerRef
    direction: Literal["inbound", "outbound"] | None = None
    content: Content | None = None
    occurred_at: datetime = Field(default_factory=utcnow)
    visibility: Visibility = Visibility.PUBLIC
    verification_hint: Verification | None = None
    canonical_type: ShortStr | None = Field(
        default=None, description="System events only, e.g. `invoice.credited`."
    )
    fields: dict[str, Any] = Field(default_factory=dict, description="Structured fields of a system event.")
    action: ActionInfo | None = None
    corrects_event_id: str | None = None
    voice: VoiceInfo | None = None
    context_stamp: ContextStamp | None = None
    usage: ModelUsage | None = Field(
        default=None, description="The model call behind an `ai_agent` message: tokens and prompt cache."
    )

    @field_validator("object_refs", mode="before")
    @classmethod
    def _shorthands(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [ObjectRef.parse(ref) if isinstance(ref, str) else ref for ref in value]
        return value

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> EventItem:
        if self.kind is EventKind.ACTION and self.action is None:
            raise ValueError("an action event needs the `action` block")
        if self.kind is not EventKind.ACTION and self.action is not None:
            raise ValueError("`action` is only valid when kind is `action`")
        if self.kind is EventKind.SYSTEM_EVENT and not self.canonical_type:
            raise ValueError("a system event needs `canonical_type`")
        if self.kind is EventKind.MESSAGE and (
            self.content is None
            or not (self.content.text or self.content.media_ref or self.content.transcript)
        ):
            raise ValueError("a message needs text, a transcript or a media reference")
        if not self.handles and not self.subjects and not self.object_refs:
            raise ValueError("an event needs at least one handle, subject or object")
        if self.usage is not None and (
            self.kind is not EventKind.MESSAGE or self.speaker.role is not Speaker.AI_AGENT
        ):
            raise ValueError("`usage` is only valid on a message of the `ai_agent`")
        return self


class IdentifyItem(Model):
    """States that several handles belong to the same subject."""

    type: Literal["identify"] = "identify"
    idempotency_key: IdStr = Field(default_factory=new_key)
    handles: list[Handle] = Field(min_length=2, max_length=16)
    method: AssertionMethod = AssertionMethod.EXPLICIT_IDENTIFY
    subject_kind: SubjectKind = SubjectKind.PERSON
    conversation_id: IdStr | None = None
    occurred_at: datetime = Field(default_factory=utcnow)


class VerifyItem(Model):
    """Raises the verification level of one conversation or task. Never inferred."""

    type: Literal["verify"] = "verify"
    idempotency_key: IdStr = Field(default_factory=new_key)
    method: VerifyMethod
    level: Verification
    conversation_id: IdStr | None = None
    task_id: IdStr | None = None
    handle: Handle
    valid_until: datetime | None = None
    occurred_at: datetime = Field(default_factory=utcnow)


class ConversationEndedItem(Model):
    type: Literal["conversation.ended"] = "conversation.ended"
    idempotency_key: IdStr = Field(default_factory=new_key)
    conversation_id: IdStr
    occurred_at: datetime = Field(default_factory=utcnow)


class TaskEndedItem(Model):
    type: Literal["task.ended"] = "task.ended"
    idempotency_key: IdStr = Field(default_factory=new_key)
    task_id: IdStr
    occurred_at: datetime = Field(default_factory=utcnow)


class HandoffItem(Model):
    """A transfer to a human or another agent."""

    type: Literal["handoff"] = "handoff"
    idempotency_key: IdStr = Field(default_factory=new_key)
    conversation_id: IdStr
    target: Literal["human", "agent"]
    target_source: ShortStr | None = None
    reason: ShortStr | None = None
    mode: Literal["warm", "cold"] = "warm"
    occurred_at: datetime = Field(default_factory=utcnow)


class HeartbeatItem(Model):
    """Periodic counters from the SDK, used by the server to compute per-source coverage."""

    type: Literal["heartbeat"] = "heartbeat"
    window_start: datetime
    sent: int = Field(ge=0)


BatchItem = Annotated[
    EventItem
    | IdentifyItem
    | VerifyItem
    | ConversationEndedItem
    | TaskEndedItem
    | HandoffItem
    | HeartbeatItem,
    Field(discriminator="type"),
]


class BatchRequest(Model):
    items: list[BatchItem] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)


class ItemError(ResponseModel):
    index: int
    code: str
    detail: str | None = None


class BatchResponse(ResponseModel):
    accepted: int
    duplicates: int
    errors: list[ItemError] = Field(default_factory=list)


class MediaUploadRequest(Model):
    content_type: ShortStr
    size_bytes: int = Field(gt=0, le=MAX_MEDIA_BYTES)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    subject: Handle | None = Field(
        default=None,
        description="Whose media it is. Stored under that person, so erasing them erases it too.",
    )


class MediaUploadResponse(ResponseModel):
    media_ref: str
    upload_url: str
    upload_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Send exactly these headers with the upload; the store refuses other bytes.",
    )
    expires_at: datetime


class FeedbackRequest(Model):
    """A correction of what Niadra derived. The server records it as a `feedback.*` system event."""

    idempotency_key: IdStr = Field(default_factory=new_key)
    subject: Handle
    action: FeedbackAction
    fact_id: str | None = None
    open_item_id: str | None = None
    conversation_id: IdStr | None = None
    value: Annotated[str, StringConstraints(max_length=2000)] | None = None
    reason: Annotated[str, StringConstraints(max_length=500)] | None = None
