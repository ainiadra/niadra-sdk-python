"""The shape of a benchmark case: who the customer is on each channel, what happened before, and the
question that proves whether the memory carried it.

Times are offsets before the probe (`days_ago`), never absolute dates, so every run seeds a history
that ends just before it asks.
"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    model_serializer,
)

Category = Literal[
    "continuity",
    "identity",
    "recurrence",
    "order_time",
    "promise_action",
    "privacy",
    "contradiction",
    "paraphrase",
    "long_history",
    "unanswerable",
    "recurrence_topic",
]
# The categories of dataset v1. Every results file lists them, even when a smoke run skipped one.
BASE_CATEGORIES: tuple[Category, ...] = (
    "continuity",
    "identity",
    "recurrence",
    "order_time",
    "promise_action",
    "privacy",
    "contradiction",
)
# Added by dataset v2 (benchmarks/README.md, "The dataset"); v1 cases never use them.
V2_CATEGORIES: tuple[Category, ...] = ("paraphrase", "long_history", "unanswerable", "recurrence_topic")
CATEGORIES: tuple[Category, ...] = BASE_CATEGORIES + V2_CATEGORIES
# Categories whose answer is a count: the history must never hold the count word itself.
COUNT_CATEGORIES: frozenset[str] = frozenset({"recurrence", "recurrence_topic"})
Channel = Literal["whatsapp", "voice", "email", "app", "crm", "erp"]
HandleKind = Literal["phone", "wa_id", "email", "app_user", "crm_id"]
Level = Literal["V0", "V1", "V2"]

# The handle each channel identifies the customer with, as its own system reports it.
CHANNEL_HANDLE: dict[str, HandleKind] = {
    "whatsapp": "wa_id",
    "voice": "phone",
    "email": "email",
    "app": "app_user",
    "crm": "crm_id",
    "erp": "crm_id",
}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Customer(_Model):
    """The stable part of a customer. Phone numbers, e-mail addresses and ids get a suffix per run and
    repetition when seeding (`identity.py`), so every repetition seeds a person no memory has seen."""

    name: str
    country_code: str
    email_user: str
    app_user: str
    crm_id: str


class Turn(_Model):
    role: Literal["customer", "agent"]
    text: str


class SystemRecord(_Model):
    """A system of record's event, or an internal agent's action on one."""

    kind: Literal["system_event", "action"]
    text: str = Field(description="The record as one line, for systems that store text.")
    canonical_type: str | None = None
    operation: str | None = None
    object_type: str
    object_id: str
    fields: dict[str, str] = Field(default_factory=dict)
    # A CRM profile names every id of the customer; other records name the CRM id only.
    links_all_handles: bool = False


class Session(_Model):
    id: str
    channel: Channel
    days_ago: float = Field(gt=0)
    turns: list[Turn] = Field(default_factory=list)
    record: SystemRecord | None = None
    sensitive: bool = Field(default=False, description="Said in a conversation verified at V2.")
    role: str = Field(default="filler", description="What the session is for in the case: key or filler.")


class Probe(_Model):
    channel: Channel
    question: str
    # The level the probe conversation proved: V1 for a known number or login, V0 for a caller with no proof.
    verification: Level
    verify_method: str | None = None


class Expectation(_Model):
    # Every group must match; any alternative in a group matches it. Matching is by whole tokens.
    all_of: list[list[str]] = Field(default_factory=list)
    # None of these may appear (privacy: the sensitive value).
    none_of: list[str] = Field(default_factory=list)
    reference_answer: str
    # Privacy cases: what a verified conversation (V2) must answer; the validity rule uses it.
    verified_all_of: list[list[str]] = Field(default_factory=list)
    # Unanswerable cases (dataset v2): what the history holds no record of, in the case's language. The
    # right answer says there is none and gives no value for it.
    no_record: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_v2_fields(
        self, handler: SerializerFunctionWrapHandler, _info: SerializationInfo
    ) -> dict[str, object]:
        # Dataset v1 is committed byte for byte: a field v2 added is written only when a case uses it.
        data: dict[str, object] = handler(self)
        if self.no_record is None:
            data.pop("no_record", None)
        return data


class Case(_Model):
    id: str
    language: Literal["pt", "en"]
    domain: str
    category: Category
    company: str
    customer: Customer
    sessions: list[Session]
    probe: Probe
    expect: Expectation

    def chronological(self) -> list[Session]:
        return sorted(self.sessions, key=lambda s: -s.days_ago)
