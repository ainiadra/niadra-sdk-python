"""Governance models: a key's identity, a customer's memory as people read it, fact history,
corrections, erasure and export. The governance routes take a key with the `admin` scope."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from niadra.models._base import IdStr, Model, ResponseModel
from niadra.models.common import Handle, ObjectRef


class KeyIdentity(ResponseModel):
    """What a key authenticates as (`GET /v1/sources/me`)."""

    space_id: str
    tenant_id: str
    region: str
    environment: str
    source_id: str
    source_name: str | None = None
    vendor: str | None = None
    key_id: str
    scopes: list[str] = Field(default_factory=list)
    audience: str
    verification_ceiling: str
    purposes: list[str] = Field(default_factory=list)
    agent_memory: bool = False


class Origin(ResponseModel):
    event_id: str | None = None
    kind: str | None = None
    source_id: str | None = None
    channel: str | None = None
    at: datetime | None = None
    speaker: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None


class FactOut(ResponseModel):
    id: str
    predicate: str
    value: str | None = Field(default=None, description="None when the policy masks it for this reader.")
    masked: bool = False
    category: str
    sensitivity: str
    status: str = Field(description="`active`, `expired`, `quarantined` or `retracted`.")
    confidence: float
    valid_at: datetime
    invalid_at: datetime | None = None
    last_confirmed_at: datetime
    times_seen: int
    about_handle_id: str | None = None
    object: ObjectRef | None = None
    evidence_event_ids: list[str] = Field(default_factory=list)
    origin: Origin | None = None


class OpenItemOut(ResponseModel):
    id: str
    description: str | None = None
    masked: bool = False
    owner: str
    promise: bool
    status: str
    due_at: datetime | None = None
    created_at: datetime
    object: ObjectRef | None = None
    expected_operation: str | None = None
    closed_by_action_id: str | None = None
    closed_at: datetime | None = None
    origin: Origin | None = None


class EpisodeOut(ResponseModel):
    id: str
    version: int
    summary: str | None = None
    masked: bool = False
    intent: str
    category: str
    normalized_category: str
    outcome: str
    resolution: str | None = None
    sentiment: float | None = None
    session_id: str
    started_at: datetime
    ended_at: datetime
    origin: Origin


class TraitEvidenceOut(ResponseModel):
    kind: str
    id: str
    origin: Origin | None = None


class TraitOut(ResponseModel):
    id: str
    name: str
    value: str | None = None
    masked: bool = False
    layer: str
    rule_version: str
    first_seen: datetime
    last_evidence_at: datetime
    expires_at: datetime
    confidence: float
    active: bool
    retracted_at: datetime | None = None
    evidence: list[TraitEvidenceOut] = Field(default_factory=list)


class ProfileMemory(ResponseModel):
    """Everything memory holds about one customer, every status included, under the reader's policy."""

    profile_id: str
    policy_version: str
    audience: str
    facts: list[FactOut] = Field(default_factory=list)
    open_items: list[OpenItemOut] = Field(default_factory=list)
    timeline: list[EpisodeOut] = Field(default_factory=list)
    traits: list[TraitOut] = Field(default_factory=list)
    withheld: dict[str, int] = Field(default_factory=dict)


class FactRelation(ResponseModel):
    from_fact_id: str
    to_fact_id: str
    type: str


class FactHistory(ResponseModel):
    """Every value one fact's slot held, oldest first, with the edges between them."""

    profile_id: str
    fact_id: str
    predicate: str
    policy_version: str
    versions: list[FactOut] = Field(default_factory=list)
    relations: list[FactRelation] = Field(default_factory=list)
    withheld: dict[str, int] = Field(default_factory=dict)


class ProfileMatch(ResponseModel):
    profile_id: str
    pseudonym: str
    kind: str
    partial: bool = False
    handles: list[dict[str, Any]] = Field(default_factory=list, description="Masked by the key's role.")
    last_seen: datetime | None = None


class ProfileSearchResult(ResponseModel):
    items: list[ProfileMatch] = Field(default_factory=list)


CorrectionAction = Literal["retract_fact", "correct_fact", "resolve_open_item"]


class CorrectionRequest(Model):
    """A person's or an admin key's correction of one customer's memory (`POST /v1/corrections`)."""

    profile_id: str
    action: CorrectionAction
    fact_id: str | None = None
    open_item_id: str | None = None
    value: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    reason: Annotated[str, StringConstraints(max_length=500)] | None = None


class ForgetRequest(Model):
    target: Literal["profile", "handle", "conversation"]
    profile_id: str | None = None
    handle: Handle | None = None
    conversation_id: IdStr | None = None


class Erasure(ResponseModel):
    request_id: str
    status: str = Field(description="`pending`, `running`, `completed` or `failed`.")
    target_kind: str
    target_id: str
    requested_at: datetime
    completed_at: datetime | None = None
    erased: dict[str, int] = Field(default_factory=dict)
    export_run_ids: list[str] = Field(default_factory=list)
    receipt_hash: str | None = None


class ExportRequest(Model):
    profile_id: str | None = None
    handle: Handle | None = None


class ExportPackage(ResponseModel):
    run_id: str
    profile_id: str
    download_url: str
    download_expires_at: datetime
    files: dict[str, str] = Field(default_factory=dict)
    sha256: str
