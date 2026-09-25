"""The emulator's agent memory: `/v1/agent-memory`, in memory, with the API's rules.

- A note with personal data is refused with 422 `personal_data_in_agent_memory` instead of being
  masked: e-mails, phone numbers, documents, card numbers and any handle the emulator has seen.
- Editing a note makes a new version that supersedes the old one, which is retired.
- With `writes="human_only"` a note from an agent waits as a proposal for a person to approve.
- The space starts with the agent memory off, as a new space does: the block answers
  `enabled: false` with the etag `am-off` and the search finds nothing, until
  `MockCell.enable_agent_memory()` (the approved `agent_memory.enabled` setting).
- Writing needs the key's `agent_memory:write` scope; the emulator's test key carries it.

Every note belongs to the emulator's one source, `MOCK_SOURCE`.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from niadra.models.agent_memory import (
    AgentMemoryBlock,
    AgentMemoryExport,
    AgentNote,
    AgentNotePage,
    AgentNoteProposal,
    AgentNoteProposalPage,
    CreateAgentNoteRequest,
    DistillRequest,
    ErasedAgentNotes,
    RememberResult,
    UpdateAgentNoteRequest,
)

MOCK_SOURCE = "00000000-0000-4000-8000-00000000a9e7"
HEADER = "From the agent itself (procedures and working notes, not customer data)"

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_DOCUMENT = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b|\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_WORD = re.compile(r"\w{2,}", re.UNICODE)


class PersonalDataError(ValueError):
    """The note carries personal data; the API refuses it instead of masking it."""


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def _tokens(text: str) -> int:
    return len(text) // 4 + 1


@dataclass
class AgentMemoryStore:
    clock: Callable[[], datetime]
    known_values: Callable[[], Iterable[str]] = tuple
    enabled: bool = False
    writes: Literal["agent", "human_only"] = "agent"
    notes: dict[str, AgentNote] = field(default_factory=dict)
    proposals: dict[str, AgentNoteProposal] = field(default_factory=dict)

    def check(self, *texts: str) -> None:
        """Refuses text with personal data, including any id the emulator has seen as a handle."""
        joined = "\n".join(texts)
        if _EMAIL.search(joined) or _PHONE.search(joined) or _DOCUMENT.search(joined):
            raise PersonalDataError
        lowered = joined.lower()
        digits = re.sub(r"\D", "", joined)
        for value in self.known_values():
            plain = re.sub(r"\D", "", value)
            if value.lower() in lowered or (len(plain) >= 7 and plain in digits):
                raise PersonalDataError

    def _active(self) -> list[AgentNote]:
        now = self.clock()
        return [
            n
            for n in self.notes.values()
            if n.status == "active" and (n.valid_until is None or n.valid_until > now)
        ]

    def block(self, max_tokens: int, tags: list[str], view: str | None) -> AgentMemoryBlock:
        """The notes as the API renders them: a header, one line per note, skipping what does not fit."""
        if not self.enabled:
            return AgentMemoryBlock(text="", etag="am-off", enabled=False)
        wanted = set(tags) | ({view} if view else set())
        ranked = sorted(
            self._active(), key=lambda n: (-len(wanted & set(n.tags)), -n.version, n.created_at, n.note_id)
        )
        lines: list[str] = []
        chosen: list[str] = []
        used = _tokens(HEADER) + 4
        for note in ranked:
            line = f"- [{note.kind}] {note.title}: {' '.join(note.body.split())}"
            if used + _tokens(line) > max_tokens:
                continue  # a shorter note further down may still fit
            lines.append(line)
            chosen.append(note.note_id)
            used += _tokens(line)
        text = f"<agent_notes>\n{HEADER}\n" + "\n".join(lines) + "\n</agent_notes>" if lines else ""
        digest = hashlib.sha256((text + "|" + ",".join(chosen)).encode()).hexdigest()[:32]
        return AgentMemoryBlock(
            text=text, notes=chosen, etag=f"am-{digest}", tokens=_tokens(text) if text else 0
        )

    def search(self, query: str, tags: list[str], limit: int) -> list[AgentNote]:
        if not self.enabled:
            return []
        words = _words(query)
        scored = []
        for note in self._active():
            if tags and not set(tags) & set(note.tags):
                continue
            score = len(words & _words(f"{note.title} {note.body} {' '.join(note.tags)}"))
            if score:
                scored.append((score, note))
        scored.sort(key=lambda pair: (-pair[0], pair[1].title))
        return [note for _, note in scored[:limit]]

    def create(self, request: CreateAgentNoteRequest, *, by: str = "agent") -> RememberResult:
        self.check(request.title, request.body, *request.tags)
        if by == "agent" and self.writes == "human_only":
            proposal = AgentNoteProposal(
                proposal_id=str(uuid.uuid4()),
                source_id=request.source_id or MOCK_SOURCE,
                kind=request.kind,
                title=request.title,
                body=request.body,
                tags=list(request.tags),
                visibility=request.visibility,
                evidence=request.evidence,
                origin="agent",
                status="pending",
                created_at=self.clock(),
            )
            self.proposals[proposal.proposal_id] = proposal
            return RememberResult(proposal_id=proposal.proposal_id)
        note = self._new_note(request.model_dump(exclude={"source_id"}), origin=by, by=by)
        return RememberResult(note=note)

    def _new_note(self, fields: dict[str, Any], *, origin: str, by: str, version: int = 1) -> AgentNote:
        note = AgentNote(
            note_id=str(uuid.uuid4()),
            source_id=MOCK_SOURCE,
            origin=origin,
            version=version,
            status="active",
            created_at=self.clock(),
            created_by=MOCK_SOURCE if by == "agent" else by,
            **{k: v for k, v in fields.items() if k in AgentNote.model_fields},
        )
        self.notes[note.note_id] = note
        return note

    def get(self, note_id: str) -> AgentNote:
        return self.notes[note_id]

    def page(
        self, status: str | None, visibility: str | None, cursor: str | None, limit: int
    ) -> AgentNotePage:
        chosen = [
            n
            for n in sorted(self.notes.values(), key=lambda n: n.created_at, reverse=True)
            if (status is None or n.status == status) and (visibility is None or n.visibility == visibility)
        ]
        offset = int(cursor or 0)
        more = offset + limit < len(chosen)
        following = str(offset + limit) if more else None
        return AgentNotePage(items=chosen[offset : offset + limit], next_cursor=following)

    def update(self, note_id: str, request: UpdateAgentNoteRequest) -> AgentNote:
        old = self.notes[note_id]
        changes = request.model_dump(exclude_none=True)
        merged = {**old.model_dump(), **changes}
        self.check(merged["title"], merged["body"], *merged["tags"])
        self.notes[note_id] = old.model_copy(update={"status": "retired"})
        fields = {
            k: merged[k] for k in ("kind", "title", "body", "tags", "visibility", "valid_until", "evidence")
        }
        return self._new_note(
            {**fields, "supersedes_note_id": note_id}, origin=old.origin, by="human", version=old.version + 1
        )

    def retire(self, note_id: str) -> AgentNote:
        retired = self.notes[note_id].model_copy(update={"status": "retired"})
        self.notes[note_id] = retired
        return retired

    def versions(self, note_id: str) -> list[AgentNote]:
        """Every version of the note's chain, oldest first."""
        chain = [self.notes[note_id]]
        while chain[0].supersedes_note_id:
            chain.insert(0, self.notes[chain[0].supersedes_note_id])
        newer = {n.supersedes_note_id: n for n in self.notes.values() if n.supersedes_note_id}
        while chain[-1].note_id in newer:
            chain.append(newer[chain[-1].note_id])
        return chain

    def erase(self, source_id: str) -> ErasedAgentNotes:
        gone = [k for k, n in self.notes.items() if n.source_id == source_id]
        for key in gone:
            del self.notes[key]
        return ErasedAgentNotes(
            source_id=source_id, deleted=len(gone), receipt_id=f"rcpt_{uuid.uuid4().hex[:16]}"
        )

    def export(self, source_id: str) -> AgentMemoryExport:
        notes = [n for n in self.notes.values() if n.source_id == source_id]
        return AgentMemoryExport(source_id=source_id, exported_at=self.clock(), notes=notes)

    def distill(self, request: DistillRequest, actions: list[str]) -> AgentNoteProposal:
        """A procedure proposed from what the agents did (operations only, never what anyone said)."""
        steps = "; ".join(dict.fromkeys(actions)) or "no recorded actions"
        body = f"Steps that were taken, in order: {steps}."
        self.check(body)
        proposal = AgentNoteProposal(
            proposal_id=str(uuid.uuid4()),
            source_id=request.source_id,
            kind="procedure",
            title="Procedure proposed from a past session",
            body=body,
            tags=sorted({a.split(" ")[0] for a in actions})[:8],
            evidence={"conversation_id": request.conversation_id, "task_id": request.task_id},
            origin="distilled",
            status="pending",
            created_at=self.clock(),
        )
        self.proposals[proposal.proposal_id] = proposal
        return proposal

    def proposal_page(self, status: str | None, cursor: str | None, limit: int) -> AgentNoteProposalPage:
        chosen = [p for p in self.proposals.values() if status is None or p.status == status]
        offset = int(cursor or 0)
        more = offset + limit < len(chosen)
        return AgentNoteProposalPage(
            items=chosen[offset : offset + limit], next_cursor=str(offset + limit) if more else None
        )

    def decide(self, proposal_id: str, approve: bool) -> AgentNote | AgentNoteProposal:
        proposal = self.proposals[proposal_id]
        if proposal.status != "pending":
            raise ValueError("already decided")
        if not approve:
            rejected = proposal.model_copy(update={"status": "rejected", "decided_at": self.clock()})
            self.proposals[proposal_id] = rejected
            return rejected
        fields = proposal.model_dump(include={"kind", "title", "body", "tags", "visibility", "evidence"})
        note = self._new_note(fields, origin=proposal.origin, by="human")
        self.proposals[proposal_id] = proposal.model_copy(
            update={"status": "approved", "decided_at": self.clock(), "note_id": note.note_id}
        )
        return note
