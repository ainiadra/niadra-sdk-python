"""Turn records in the emulator: `POST /v1/turns`, `GET /v1/turns/{turn_id}` and `POST /v1/turns/promote`.

The emulator checks what the server checks (the Turn Record spec, sections 5.5 and 6), so a record the SDK
builds wrong fails here the same way:

- each turn is read through the turn record model, and one that does not read is refused by its index;
- every blob key a record uses has its blob, no `call_id` repeats, `ended_at` is not before `started_at`,
  and each blob has the form its content mode allows;
- a stored value must be the one its digest names;
- a source records `recording_mode` or less: a turn that keeps more is refused with `content_mode_refused`;
- the same turn sent twice is one turn.

Every turn lands in the short tier; a flagged turn is kept at once, and `promote` keeps others on request.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from niadra.models.events import ItemError
from niadra.models.turns import (
    PromoteRequest,
    PromoteResponse,
    TurnRecord,
    TurnsResponse,
    TurnView,
)
from niadra.turns.digest import digest

KEEPS = (
    "error",
    "guard_acted",
    "handoff",
    "assertion_failed",
    "synthetic",
    "incomplete",
    "negative_feedback",
)
"""Flags that send a turn to the kept tier at capture."""
_PRIVACY = {"stored": 0, "pointer": 1, "hash_only": 2}
KEPT_DAYS = 30


class RefusedError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class StoredTurn:
    record: dict[str, Any]
    kept: bool


@dataclass
class TurnStore:
    """The turns one emulated space recorded."""

    recording_mode: str = "stored"
    """The source's content mode: a turn may keep that or less."""
    required_pins: frozenset[str] = frozenset({"prompts", "model"})
    turns: dict[str, StoredTurn] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, items: list[Any]) -> TurnsResponse:
        errors: list[ItemError] = []
        accepted = duplicates = 0
        with self._lock:
            for index, raw in enumerate(items):
                try:
                    document = self._check(raw)
                except RefusedError as refused:
                    errors.append(ItemError(index=index, code=refused.code, detail=refused.detail))
                    continue
                turn_id = document["turn_id"]
                if turn_id in self.turns:
                    duplicates += 1
                    continue
                kept = any(flag in KEEPS for flag in document.get("flags", ()))
                self.turns[turn_id] = StoredTurn(document, kept)
                accepted += 1
        return TurnsResponse(accepted=accepted, duplicates=duplicates, errors=errors)

    def read(self, turn_id: str) -> TurnView | None:
        with self._lock:
            stored = self.turns.get(turn_id)
        if stored is None:
            return None
        record = TurnRecord.model_validate(stored.record)
        pins = stored.record.get("build", {}).get("pins", {})
        return TurnView(
            record=record,
            replayable=all(pins.get(pin) for pin in self.required_pins),
            tier="kept" if stored.kept else "short",
            kept_until=datetime.now(timezone.utc) + timedelta(days=KEPT_DAYS) if stored.kept else None,
        )

    def promote(self, request: PromoteRequest) -> PromoteResponse:
        promoted = already = missing = 0
        with self._lock:
            if request.conversation_id is not None:
                chosen = [
                    t
                    for t in self.turns.values()
                    if t.record.get("conversation_id") == request.conversation_id
                ]
            else:
                chosen = []
                for turn_id in request.turn_ids:
                    stored = self.turns.get(turn_id)
                    if stored is None:
                        missing += 1
                    else:
                        chosen.append(stored)
            for stored in chosen:
                if stored.kept:
                    already += 1
                else:
                    stored.kept = True
                    promoted += 1
        return PromoteResponse(promoted=promoted, already_kept=already, not_found=missing)

    def _check(self, raw: Any) -> dict[str, Any]:
        try:
            record = TurnRecord.model_validate(raw)
        except ValidationError as exc:
            fields = "; ".join(".".join(map(str, e["loc"])) + ": " + e["type"] for e in exc.errors())
            raise RefusedError("invalid_input", fields[:1000]) from None
        document = record.model_dump(mode="json", by_alias=True, exclude_none=True)
        _rules(record, document)
        if _PRIVACY[record.content_mode] < _PRIVACY[self.recording_mode]:
            raise RefusedError(
                "content_mode_refused",
                f"this source records `{self.recording_mode}`: a turn keeps that or less",
            )
        if record.content_mode == "stored":
            for key, blob in document.get("blobs", {}).items():
                if "content" not in blob:
                    continue
                sha256, size = digest(blob["content"])
                if sha256 != blob["sha256"] or blob.get("size", size) != size:
                    raise RefusedError(
                        "blob_digest_mismatch", f"blob {key}: the content does not match its digest"
                    )
        return document


def _rules(record: TurnRecord, document: dict[str, Any]) -> None:
    """The rules of the record the schema cannot state (Turn Record 5.5 and 6.1)."""
    if record.conversation_id is None and record.task_id is None:
        raise RefusedError("invalid_input", "a turn belongs to a conversation or a task")
    if record.ended_at is not None and record.ended_at < record.started_at:
        raise RefusedError("invalid_input", "`ended_at` is before `started_at`")
    for blob in record.blobs.values():
        if record.content_mode == "pointer" and (blob.pointer is None or blob.content is not None):
            raise RefusedError("invalid_input", "in `pointer` mode every blob has a pointer and no content")
        if record.content_mode == "hash_only" and (blob.pointer is not None or blob.content is not None):
            raise RefusedError("invalid_input", "in `hash_only` mode a blob is only its hash")
        if record.content_mode == "stored" and blob.pointer is not None:
            raise RefusedError("invalid_input", "in `stored` mode a blob travels as content, not a pointer")
    used = {
        k
        for call in document.get("calls", ())
        for k in (call.get(f) for f in ("args", "result_model", "result_ui"))
        if k
    }
    used |= {r["blob"] for r in document.get("reads", ()) if r.get("blob")}
    used |= {v for k, v in document.get("output", {}).items() if k in ("ui", "document") and v}
    evidence = [c.get("evidence") or {} for c in document.get("claims", ())]
    used |= {e["anchor"] for e in evidence if e.get("anchor")}
    missing = sorted(used - set(document.get("blobs", {})))
    if missing:
        raise RefusedError("invalid_input", "blob keys without a blob: " + ", ".join(missing))
    ids = [call["call_id"] for call in document.get("calls", ())]
    if len(ids) != len(set(ids)):
        raise RefusedError("invalid_input", "a `call_id` repeats")
