"""The record of a closed turn as it travels (`turn-record.v0`), built on the sender, off the agent's path.

Here each value gets its digest (SHA-256 over its canonical JSON, `niadra.turns.digest`) and takes the
form of the content mode:

- `stored`: the value travels in the record, and Niadra keeps it encrypted;
- `pointer`: the value goes to the company's storage first (`niadra.turns.store`), and only the pointer and
  the digest travel. A record whose values cannot all be pointed at leaves as `hash_only`, `partial`;
- `hash_only`: only digests travel.

A value the queue had to drop leaves as its digest when the sender computed it before, and otherwise
leaves the record, with the call fields that named it. Either way the record says `partial`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from niadra._version import __version__
from niadra.turns.capture import Blob, TurnFrame
from niadra.turns.digest import canonical, digest
from niadra.turns.store import BlobStore

logger = logging.getLogger("niadra")

ContentMode = Literal["stored", "pointer", "hash_only"]
SDK = f"niadra-python/{__version__}"

MAX_CALLS = 500
MAX_BLOBS = 1000
MAX_READS = 20
MAX_CLAIMS = 500
MAX_EVENT_KEYS = 50
MAX_DECISIONS = 50
MAX_INTERACTIONS = 200
BLOB_FIELDS = ("args", "result_model", "result_ui")


def _null(_: str) -> None:
    """NaN and the infinities, which canonical JSON has no form for, are kept as null."""
    return


def blob_value(blob: Blob) -> tuple[bool, Any]:
    """The blob's value, when the queue still holds it, with its digest computed and kept on the blob."""
    if blob.data is None:
        return False, None
    value = json.loads(blob.data, parse_constant=_null)
    if blob.sha256 is None:
        blob.sha256, blob.canonical_size = digest(value)
    return True, value


def build(
    frame: TurnFrame,
    mode: ContentMode,
    *,
    store: BlobStore | None = None,
    claims: Callable[[TurnFrame], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """The wire record of `frame` in `mode`. Never raises for a value: one that fails is left out and the
    record says what it lost."""
    completeness = frame.completeness
    blobs: dict[str, dict[str, Any]] = {}
    unpointed = False
    for key, blob in list(frame.blobs.items())[:MAX_BLOBS]:
        try:
            present, value = blob_value(blob)
        except (TypeError, ValueError, UnicodeError):
            logger.debug("niadra: a value of turn %s has no canonical form", frame.turn_id, exc_info=True)
            completeness = "incomplete"
            continue
        if blob.sha256 is None:
            continue
        entry: dict[str, Any] = {"sha256": blob.sha256, "size": blob.canonical_size}
        if mode == "stored" and present:
            entry["content"] = value
        elif mode == "pointer":
            if blob.pointer is None and present and store is not None:
                blob.pointer = _put(store, frame.turn_id, key, value)
            if blob.pointer is None:
                unpointed = True
            entry["pointer"] = blob.pointer
        if not present and completeness == "complete":
            completeness = "partial"
        blobs[key] = entry
    if mode == "pointer" and unpointed:
        mode = "hash_only"
        completeness = "partial" if completeness == "complete" else completeness
    if mode != "stored":
        blobs = {k: {"sha256": b["sha256"], "size": b["size"], **_pointer(b, mode)} for k, b in blobs.items()}
    calls = [_call(entry, blobs) for entry in frame.calls[:MAX_CALLS]]
    found = _claims(frame, claims)
    if found is None:
        completeness = "incomplete"
    lost = len(frame.calls) > MAX_CALLS or len(frame.blobs) > MAX_BLOBS or len(frame.reads) > MAX_READS
    if lost and completeness == "complete":
        completeness = "partial"
    agent: dict[str, Any] = {"name": frame.agent}
    if frame.role:
        agent["role"] = frame.role
    if frame.parent_turn_id:
        agent["parent_turn_id"] = frame.parent_turn_id
    flags = set(frame.flags)
    if completeness == "incomplete":
        flags.add("incomplete")
    record: dict[str, Any] = {
        "spec": "turn-record.v0",
        "turn_id": frame.turn_id,
        "agent": agent,
        "kind": frame.kind,
        "started_at": frame.started_at.isoformat(),
        "fidelity": "gold",
        "completeness": completeness,
        "content_mode": mode,
        "build": {"pins": frame.pins, "sdk": SDK, **({"adapter": frame.adapter} if frame.adapter else {})},
        "reads": frame.reads[:MAX_READS],
        "calls": calls,
        "claims": (found or [])[:MAX_CLAIMS],
        "interactions": frame.interactions[:MAX_INTERACTIONS],
        "coordination": frame.coordination[:MAX_DECISIONS],
        "effects": [{"key": k, "state": v} for k, v in list(frame.effects.items())[:MAX_DECISIONS]],
        "output": {
            "event_keys": frame.event_keys[:MAX_EVENT_KEYS],
            **({"handoff_id": frame.handoff_id} if frame.handoff_id else {}),
        },
        "flags": sorted(flags),
        "blobs": blobs,
    }
    if frame.conversation_id:
        record["conversation_id"] = frame.conversation_id
    else:
        record["task_id"] = frame.task_id
    if frame.ended_at is not None:
        record["ended_at"] = frame.ended_at.isoformat()
        record["latency_ms"] = frame.latency_ms
    return record


def as_hash_only(record: dict[str, Any]) -> dict[str, Any]:
    """The same record with digests only: what any source accepts, and what fits any request."""
    blobs = {k: {"sha256": b["sha256"], "size": b["size"]} for k, b in record["blobs"].items()}
    had_values = any("content" in b or "pointer" in b for b in record["blobs"].values())
    completeness = record["completeness"]
    if had_values and completeness == "complete":
        completeness = "partial"
    return {**record, "content_mode": "hash_only", "blobs": blobs, "completeness": completeness}


def _pointer(blob: dict[str, Any], mode: ContentMode) -> dict[str, Any]:
    return {"pointer": blob["pointer"]} if mode == "pointer" else {}


def _put(store: BlobStore, turn_id: str, key: str, value: Any) -> str | None:
    try:
        return store(f"{turn_id}/{key.replace(':', '-')}.json", canonical(value))
    except Exception:
        logger.warning("niadra: a turn value could not be written to the company's store", exc_info=True)
        return None


def _call(entry: dict[str, Any], blobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    call = {k: v for k, v in entry.items() if k not in BLOB_FIELDS or v in blobs}
    if call.get("args") in blobs:
        call["args_hash"] = blobs[call["args"]]["sha256"]
    return call


def _claims(
    frame: TurnFrame, claims: Callable[[TurnFrame], list[dict[str, Any]]] | None
) -> list[dict[str, Any]] | None:
    """The claims found at capture, then the ones the claim check finds now; None when the check failed."""
    found = list(frame.claims)
    if claims is None or not frame.said:
        return found
    try:
        return found + claims(frame)
    except Exception:
        logger.warning("niadra: the claim check of turn %s failed", frame.turn_id, exc_info=True)
        return None
