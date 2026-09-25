"""The agent's own memory: `/v1/agent-memory`.

Working notes an agent keeps about its work (procedures, how the company's tools and processes
behave, pitfalls), never about a customer: the API refuses a note with personal data (422
`personal_data_in_agent_memory`). The block goes into the prompt after the agent's instructions
and before the customer's context.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from niadra.models._base import IdStr, Model, ResponseModel

AgentNoteKind = Literal["procedure", "tool_note", "process_note", "pitfall"]
AgentNoteVisibility = Literal["source", "vendor", "space"]
AgentNoteOrigin = Literal["agent", "human", "distilled"]
AgentNoteStatus = Literal["active", "retired"]
ProposalStatus = Literal["drafting", "pending", "approved", "rejected", "failed"]
Tag = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,63}$")]


class Evidence(Model):
    """Where a note came from: only the id of the conversation or task, never its text."""

    conversation_id: IdStr | None = None
    task_id: IdStr | None = None


class AgentNote(ResponseModel):
    note_id: str
    source_id: str
    visibility: AgentNoteVisibility | str
    kind: AgentNoteKind | str
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    evidence: Evidence | None = None
    origin: AgentNoteOrigin | str
    version: int
    supersedes_note_id: str | None = None
    status: AgentNoteStatus | str
    valid_until: datetime | None = None
    created_at: datetime
    created_by: str


class AgentMemoryBlock(ResponseModel):
    """`GET /v1/agent-memory/block`: the notes as one text for the prompt."""

    text: str = ""
    notes: list[str] = Field(default_factory=list)
    etag: str = ""
    tokens: int = 0
    enabled: bool = True


class AgentMemory(AgentMemoryBlock):
    """What `agent_memory()` returns. Put `text` after your instructions, before the customer's context.

    `source` says where it came from: `network`, `cache` (revalidated or within the cache's TTL)
    or `empty`. With the agent memory off in the space (or the route not there yet) `enabled` is
    False and `text` is empty; on a failure `error` says why. Either way, carry on without it.
    """

    source: Literal["network", "cache", "empty"] = "network"
    error: str | None = None

    def __bool__(self) -> bool:
        return bool(self.text)

    @classmethod
    def empty(cls, *, enabled: bool = True, error: str | None = None) -> AgentMemory:
        return cls(enabled=enabled, source="empty", error=error)


class AgentMemorySearchRequest(Model):
    query: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    tags: list[Tag] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=5, ge=1, le=20)
    conversation_id: IdStr | None = None
    task_id: IdStr | None = None


class AgentMemorySearchResponse(ResponseModel):
    notes: list[AgentNote] = Field(default_factory=list)


class CreateAgentNoteRequest(Model):
    kind: AgentNoteKind
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    body: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    tags: list[Tag] = Field(default_factory=list, max_length=8)
    evidence: Evidence | None = None
    visibility: AgentNoteVisibility = "source"
    valid_until: datetime | None = None
    source_id: str | None = None


class UpdateAgentNoteRequest(Model):
    kind: AgentNoteKind | None = None
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    body: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    tags: list[Tag] | None = None
    visibility: AgentNoteVisibility | None = None
    valid_until: datetime | None = None


class RememberResult(ResponseModel):
    """`POST /v1/agent-memory/notes`: the note, or the id of a proposal waiting for a person.

    `error` is the SDK's: `personal_data_in_agent_memory` when the API refused the note because it
    has personal data, another code on other failures.
    """

    note: AgentNote | None = None
    proposal_id: str | None = None
    error: str | None = None

    def __bool__(self) -> bool:
        return self.error is None and (self.note is not None or self.proposal_id is not None)


class AgentNotePage(ResponseModel):
    items: list[AgentNote] = Field(default_factory=list)
    next_cursor: str | None = None


class DistillRequest(Model):
    source_id: str
    conversation_id: IdStr | None = None
    task_id: IdStr | None = None


class AgentNoteProposal(ResponseModel):
    proposal_id: str
    source_id: str
    kind: AgentNoteKind | str
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    visibility: AgentNoteVisibility | str = "source"
    evidence: Evidence | None = None
    origin: Literal["distilled", "agent"] | str
    status: ProposalStatus | str
    note_id: str | None = None
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    problem: str | None = Field(
        default=None,
        description="Why a distillation failed: `nothing_to_propose`, `personal_data`, `no_turns`.",
    )


class AgentNoteProposalPage(ResponseModel):
    items: list[AgentNoteProposal] = Field(default_factory=list)
    next_cursor: str | None = None


class AgentMemoryExport(ResponseModel):
    source_id: str
    exported_at: datetime
    notes: list[AgentNote] = Field(default_factory=list)


class ErasedAgentNotes(ResponseModel):
    source_id: str
    deleted: int
    receipt_id: str | None = None
