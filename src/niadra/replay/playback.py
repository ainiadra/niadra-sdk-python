"""What a replayed turn answers with: the recorded tool results, matched by the digest of their arguments, the
pack of the time, and where what the agent says and declares goes instead of Niadra (the replay spec, 6)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from niadra.models.coordination import CheckRequest, CheckResult
from niadra.models.results import Context
from niadra.turns.capture import Said, TurnFrame, current_turn, snapshot
from niadra.turns.digest import digest

Mode = Literal["hermetic_turn", "hermetic_conversation", "era_memory"]


class BlobError(Exception):
    """A recorded value that could not be read, or is not the value its digest names."""


@dataclass(frozen=True)
class Played:
    """How one tool call answers: `live` runs the tool; otherwise `value` is the answer, recorded or empty."""

    live: bool = False
    value: Any = None
    recorded: bool = False


@dataclass
class _Recorded:
    args_hash: str | None
    value: Any
    used: bool = False


@dataclass
class Playback:
    """One execution's answers and what it collected."""

    calls: dict[str, list[_Recorded]]
    mode: Mode = "hermetic_turn"
    context: Context | None = None
    divergent: int = 0
    said: list[str] = field(default_factory=list)
    done: dict[str, int] = field(default_factory=dict)
    """Effect keys the agent declared done, and how many times."""
    handoff: bool = False
    states: dict[tuple[str, str, str], tuple[dict[str, Any], int]] = field(default_factory=dict)
    """The agent's working state in this execution, body and version by scope and agent: what the recorded
    turn read first, then what the execution wrote."""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def muted(self, item: Any) -> bool:
        """An item the replayed agent tried to send: it never leaves, and a handoff is noted."""
        kind = item.get("type") if isinstance(item, Mapping) else getattr(item, "type", None)
        if str(getattr(kind, "value", kind)) == "handoff":
            self.handoff = True
        return True

    def say(self, frame: TurnFrame, text: str) -> None:
        """What the replayed agent said: the text its assertions read, and its claims."""
        with self._lock:
            self.said.append(text)
        frame.said.append(Said(text, "chat", False, frame.agent))

    def checked(self, request: CheckRequest) -> CheckResult:
        """A check the replayed agent made, answered here: an effect it already declared done is denied, and
        anything else goes. Nothing is reserved."""
        with self._lock:
            done = request.effect_key is not None and self.done.get(request.effect_key, 0) > 0
        effect = None
        if request.effect_key is not None:
            effect = {"state": "done" if done else "none", "attempt": 1}
        return CheckResult.model_validate(
            {
                "decision": "deny" if done else "allow",
                "decision_id": str(uuid4()),
                "reasons": ["effect_done"] if done else [],
                "valid_for_s": 0,
                **({"effect": effect} if effect is not None else {}),
            }
        )

    def declared(self, kind: str, detail: Mapping[str, Any]) -> None:
        """A declaration the replayed agent made: kept here, never sent."""
        if kind == "effect" and detail.get("state") == "done":
            with self._lock:
                self.done[detail["effect_key"]] = self.done.get(detail["effect_key"], 0) + 1
        if kind == "handoff":
            self.handoff = True

    def answer(self, tool: str, arguments: Any, *, dry_run: bool) -> Played:
        """A recorded call of `tool` with the same arguments, the recorded ones of one tool taken in order;
        else the tool runs when it is safe to run dry (in `era_memory`, first), or answers empty."""
        if self.mode == "era_memory" and dry_run:
            return Played(live=True)
        try:
            wanted: str | None = digest(json.loads(snapshot(arguments)))[0]
        except (TypeError, ValueError):
            wanted = None
        with self._lock:
            for recorded in self.calls.get(tool, ()):
                if not recorded.used and recorded.args_hash == wanted:
                    recorded.used = True
                    return Played(value=recorded.value, recorded=True)
            self.divergent += 1
        return Played(live=True) if dry_run else Played()


def replaying() -> Playback | None:
    """The playback of the turn replayed in this task or thread, if any."""
    frame = current_turn()
    return frame.playback if frame is not None else None


def playback(
    record: Mapping[str, Any], read: Callable[[str], bytes] | None, mode: Mode = "hermetic_turn"
) -> Playback:
    """The answers of a recorded turn. Every value used is fetched (by pointer, inside the company's boundary)
    and must match its digest; raises `BlobError` otherwise."""
    blobs = record.get("blobs") or {}
    values: dict[str, Any] = {}

    def value(key: str | None) -> Any:
        if key is None or key not in blobs:
            return None
        if key not in values:
            values[key] = _materialized(blobs[key], read)
        return values[key]

    calls: dict[str, list[_Recorded]] = {}
    for call in record.get("calls") or []:
        if call.get("kind") != "tool" or call.get("status", "ok") != "ok" or "result_model" not in call:
            continue
        recorded = _Recorded(call.get("args_hash"), value(call["result_model"]))
        calls.setdefault(call["name"], []).append(recorded)
    pack = next((r for r in record.get("reads") or [] if r.get("surface") == "pack"), None)
    context = None
    if pack is not None and pack.get("blob") in blobs:
        try:
            context = Context.model_validate(value(pack["blob"]))
        except ValueError:
            context = None
    return Playback(calls, mode, context, states=_states(record, value))


def _states(record: Mapping[str, Any], value: Callable[[str | None], Any]) -> dict[tuple[str, str, str], Any]:
    """The working state each scope and agent held when the recorded turn first read it: where a replay
    starts."""
    states: dict[tuple[str, str, str], Any] = {}
    for read in record.get("reads") or []:
        if read.get("surface") != "agent_state" or not read.get("blob"):
            continue
        held = value(read["blob"])
        if not isinstance(held, Mapping):
            continue
        scope = held.get("scope") or {}
        key = (str(scope.get("kind")), str(scope.get("id")), str(held.get("agent")))
        states.setdefault(key, (dict(held.get("body") or {}), int(held.get("version") or 0)))
    return states


def _materialized(blob: Mapping[str, Any], read: Callable[[str], bytes] | None) -> Any:
    if "content" in blob:
        found = blob["content"]
    elif blob.get("pointer"):
        if read is None:
            raise BlobError("a value kept by pointer needs a reader: niadra.content.register(), or read=")
        try:
            found = json.loads(read(blob["pointer"]))
        except Exception as exc:
            raise BlobError("a value kept by pointer could not be read") from exc
    else:
        raise BlobError("the record keeps only the digest of a value the replay needs")
    if digest(found)[0] != blob.get("sha256"):
        raise BlobError("a recorded value does not match its digest")
    return found
