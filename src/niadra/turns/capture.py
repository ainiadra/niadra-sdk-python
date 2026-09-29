"""A turn in the agent's process: the frame that collects what one turn read, called, said and decided.

Capture never delays the agent and never fails it. At the moment something happens the frame copies it:
arguments and results become JSON bytes right away (`pydantic_core.to_json`, a deep copy by construction),
so a later change to the agent's objects never reaches the record. Digests, the claim check and sending
happen later, on the sender, off the agent's path. When the capture itself fails, the turn goes on and its
record says `completeness: incomplete`.

The frame in progress is a context variable, `niadra_turn`:

- an asyncio task copies the context when it is created, so `create_task` and `gather` inside a turn see it;
- a thread does not inherit it: `bind(fn)` runs `fn` in a copy of the caller's context, for thread pools;
- a frame opened while another is current is a sub-turn (a sub-agent, an agent used as a tool): its own
  `turn_id`, and `agent.parent_turn_id` naming the outer one. Two sub-agents running in parallel each run
  in their own copy of the context, so their calls never land in each other's frame.

A second variable holds the call in progress, so a call made inside a tool names it as `parent_call_id`.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel
from pydantic_core import to_json

from niadra._ids import new_key

if TYPE_CHECKING:
    from niadra.models.signals import ConstraintsBlock
    from niadra.replay.playback import Playback, Played
    from niadra.turns.recorder import TurnRecorder

logger = logging.getLogger("niadra")

TurnKind = Literal["message", "action", "event", "timer"]
CallStatus = Literal["ok", "error", "timeout", "cancelled"]
Flag = Literal[
    "error",
    "guard_acted",
    "guard_budget_exceeded",
    "handoff",
    "assertion_failed",
    "synthetic",
    "incomplete",
    "negative_feedback",
    "truncated",
]

_turn: ContextVar[TurnFrame | None] = ContextVar("niadra_turn", default=None)
_call: ContextVar[CallCapture | None] = ContextVar("niadra_call", default=None)

F = TypeVar("F", bound=Callable[..., Any])

# The size the queue counts for a frame besides its blobs, and for each of its calls.
FRAME_BYTES = 512
CALL_BYTES = 256


def current_turn() -> TurnFrame | None:
    """The turn open in this task or thread, if any."""
    return _turn.get()


def current_call() -> CallCapture | None:
    """The call in progress in this task or thread, if any."""
    return _call.get()


def bind(fn: F) -> F:
    """`fn`, to run in a copy of the context it was bound in: what a thread pool needs to see the turn.

    Each call runs in its own copy, so two threads can run the bound function at once.
    """
    captured = contextvars.copy_context()

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        return captured.copy().run(fn, *args, **kwargs)

    return run  # type: ignore[return-value]


def snapshot(value: Any) -> bytes:
    """`value` as JSON bytes, now: the copy a record keeps. An object JSON has no form for becomes its
    `str()`. Raises when the value cannot be copied at all."""
    if isinstance(value, BaseModel):
        return value.model_dump_json(by_alias=True).encode()
    return to_json(value, serialize_unknown=True)


@dataclass(slots=True)
class Blob:
    """A value of the record, copied at the moment: its JSON bytes until the queue drops them, and its digest
    once the sender computed it."""

    data: bytes | None
    size: int
    sha256: str | None = None
    canonical_size: int | None = None
    pointer: str | None = None
    """Where the company's store keeps it, once written (`pointer` mode)."""


@dataclass(frozen=True, slots=True)
class StateValue:
    """A field of an object a read served the turn: the claim check's evidence, with whether it may back a
    claim now (`claim_safe`) and the gaps its object declares."""

    ref: str
    field: str
    value: Any
    claim_safe: bool
    role: str | None = None
    declared_gaps: tuple[str, ...] = ()


@dataclass(slots=True)
class Said:
    """An output of the turn, kept for the claim check that runs on the sender."""

    text: str
    context: str
    immutable: bool
    agent: str | None


def _status_of(error: BaseException) -> CallStatus:
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    return "error"


class CallCapture:
    """One tool or model call of a turn, open until `result()` or `failed()`. As a context manager it is
    the call in progress, so calls made inside it name it as their parent, and an exception fails it."""

    def __init__(self, frame: TurnFrame, entry: dict[str, Any]) -> None:
        self.frame = frame
        self.entry = entry
        self.call_id: str = entry["call_id"]
        self._started = time.perf_counter()
        self._done = False
        self._token: Token[CallCapture | None] | None = None
        self.played: Played | None = None
        """In a replay: how the call answers (`niadra.replay`)."""
        self.measure: Callable[[Any], dict[str, Any] | None] | None = None
        """What the call's result honored of the constraints block, when the tool has a binding."""

    def result(
        self,
        value: Any,
        *,
        ui: Any = None,
        observations: Iterable[Mapping[str, Any]] | None = None,
        cost_units: Mapping[str, int] | None = None,
        cache_hit: bool | None = None,
    ) -> None:
        """The call returned `value`, as the model saw it. `ui` is the form the interface got, when it
        differs. `observations` are the objects the result showed, each `{ref, fields, provenance}`."""
        if self._done:
            return
        extra: dict[str, Any] = {"result_model": self.frame._blob(value)}
        if self.measure is not None:
            try:
                extra["applied"] = self.measure(value)
            except Exception:
                logger.debug("niadra: the constraints a call honored could not be measured", exc_info=True)
                self.frame.incomplete()
        if ui is not None:
            extra["result_ui"] = self.frame._blob(ui)
        if observations:
            extra["observations"] = [dict(o) for o in observations]
        if cost_units:
            extra["cost_units"] = dict(cost_units)
        if cache_hit is not None:
            extra["cache_hit"] = cache_hit
        self._finish("ok", extra)

    def failed(self, error: BaseException | CallStatus = "error") -> None:
        """The call ended without a result: an exception, or `timeout` or `cancelled`."""
        if self._done:
            return
        status = error if isinstance(error, str) else _status_of(error)
        self._finish(status, {})
        if status == "error":
            self.frame.flag("error")

    def _finish(self, status: CallStatus, extra: dict[str, Any]) -> None:
        self._done = True
        latency = int((time.perf_counter() - self._started) * 1000)
        with self.frame._lock:
            self.entry.update({k: v for k, v in extra.items() if v is not None})
            self.entry["status"] = status
            self.entry["latency_ms"] = latency

    @property
    def done(self) -> bool:
        return self._done

    def __enter__(self) -> CallCapture:
        self._token = _call.set(self)
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc is not None:
            self.failed(exc)
        _reset(_call, self._token)
        self._token = None

    async def __aenter__(self) -> CallCapture:
        return self.__enter__()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.__exit__(exc_type, exc, tb)


def _reset(var: ContextVar[Any], token: Token[Any] | None) -> None:
    if token is None:
        return
    try:
        var.reset(token)
    except ValueError:
        var.set(None)  # left from another context: nothing better to restore


class TurnFrame:
    """The record of one turn while it runs. `close()` (or leaving its `with` block) ends it and hands it to
    the turn queue; nothing else happens on the agent's path.

    Every method is safe to call from any thread of the turn, never raises for a failure of the capture
    itself, and does nothing once the frame is closed.
    """

    def __init__(
        self,
        recorder: TurnRecorder | None,
        *,
        agent: str,
        role: str | None = None,
        kind: TurnKind = "message",
        conversation_id: str | None = None,
        task_id: str | None = None,
        pins: Mapping[str, Any] | None = None,
        adapter: str | None = None,
        turn_id: str | None = None,
        parent: TurnFrame | None = None,
    ) -> None:
        self._recorder = recorder
        self.turn_id = turn_id or new_key()
        self.parent_turn_id = parent.turn_id if parent is not None else None
        self.agent = agent
        self.role = role
        self.kind: TurnKind = kind
        self.conversation_id: str | None = conversation_id or (parent.conversation_id if parent else None)
        self.task_id: str | None = task_id or (parent.task_id if parent and not conversation_id else None)
        self.pins: dict[str, Any] = dict(pins or {})
        self.adapter = adapter
        self.started_at = datetime.now(timezone.utc)
        self.ended_at: datetime | None = None
        self.latency_ms: int | None = None
        self.calls: list[dict[str, Any]] = []
        self.blobs: dict[str, Blob] = {}
        self.reads: list[dict[str, Any]] = []
        self.said: list[Said] = []
        self.state: list[StateValue] = []
        """Fields of objects the turn read from state, for the claim check: a value with its freshness."""
        self._guarded: set[str] = set()
        """Outputs the claim guard already checked: the sender does not count them again."""
        self.claims: list[dict[str, Any]] = []
        self.interactions: list[dict[str, Any]] = []
        self.coordination: list[dict[str, str]] = []
        self.effects: dict[str, str] = {}
        self.event_keys: list[str] = []
        self.flags: set[str] = set()
        self.completeness: Literal["complete", "partial", "incomplete"] = "complete"
        self.handoff_id: str | None = None
        self.constraints: ConstraintsBlock | None = None
        """The constraints block the turn read last: what its tools' calls are measured against."""
        self.playback: Playback | None = parent.playback if parent is not None else None
        """Set in a replay: recorded answers for the tools, and where what the turn says goes. A sub-turn of a
        replayed turn (a framework's run inside it) is replayed with it, and never sent."""
        self.mode: Literal["stored", "pointer", "hash_only"] | None = None
        """The content mode this turn must leave in, when the server refused the recorder's."""
        self.closed = False
        self._lock: threading.Lock = threading.Lock()
        self._counters = {"b": 0, "k": 0, "m": 0}
        self._adoptable: list[CallCapture] = []
        self._token: Token[TurnFrame | None] | None = None

    # Capture

    def tool_call(
        self,
        name: str,
        args: Any = None,
        *,
        call_id: str | None = None,
        attempt: int = 1,
        synthetic: bool = False,
        cache_hit: bool = False,
        effect_key: str | None = None,
        adoptable: bool = False,
    ) -> CallCapture:
        """Opens a tool call, copying its arguments now. `call_id` is the provider's id when there is one;
        otherwise the frame names it `k1`, `k2`, ... Use the result as a context manager, or call its
        `result()` or `failed()`."""
        entry: dict[str, Any] = {
            "call_id": call_id or f"k{self._next('k')}",
            "kind": "tool",
            "name": name,
            "attempt": attempt,
            "synthetic": synthetic,
            "cache_hit": cache_hit,
        }
        if effect_key is not None:
            entry["effect_key"] = effect_key
        parent = _call.get()
        if parent is not None and parent.frame is self:
            entry["parent_call_id"] = parent.call_id
        if args is not None:
            entry["args"] = self._blob(args)
        if synthetic:
            self.flag("synthetic")
        call = self._add(entry)
        if adoptable:
            with self._lock:
                self._adoptable.append(call)
        return call

    def adopt(self, name: str, args: Any) -> CallCapture | None:
        """The open call of `name` an adapter recorded from its framework's callbacks, taken over by the
        `@niadra.tool` wrapper running inside it, so the call is recorded once, with the wrapper's
        arguments."""
        with self._lock:
            call = next((c for c in self._adoptable if not c.done and c.entry.get("name") == name), None)
            if call is None:
                return None
            self._adoptable.remove(call)
            before = call.entry.pop("args", None)
            if before is not None:
                self.blobs.pop(before, None)
        key = self._blob(args)
        if key is not None:
            with self._lock:
                call.entry["args"] = key
        return call

    def model_call(
        self,
        model: str | None,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        tokens_cached: int = 0,
        latency_ms: int | None = None,
        call_id: str | None = None,
        status: CallStatus = "ok",
    ) -> None:
        """Records a model call that already ended, with its tokens when the provider reported them. The
        first model named becomes the turn's `model` pin unless the build set one."""
        entry: dict[str, Any] = {
            "call_id": call_id or f"m{self._next('m')}",
            "kind": "model",
            "status": status,
        }
        if model:
            entry["name"] = model
            self.pins.setdefault("model", model)
        if tokens_in is not None and tokens_out is not None:
            entry["tokens"] = {"in": tokens_in, "cached": tokens_cached, "out": tokens_out}
        if latency_ms is not None:
            entry["latency_ms"] = latency_ms
        parent = _call.get()
        if parent is not None and parent.frame is self:
            entry["parent_call_id"] = parent.call_id
        self._add(entry)

    def pin_model(self, model: str) -> None:
        """The model the turn called, as its `model` pin, unless the build named one."""
        with self._lock:
            self.pins.setdefault("model", model)

    def read(
        self,
        surface: str,
        *,
        etag: str | None = None,
        version: str | None = None,
        receipt_id: str | None = None,
        value: Any = None,
    ) -> None:
        """A read the turn made from Niadra, by its version: the pack by ETag, a block by its version.
        `value` is what the read served, kept as a blob of the record so a replay and the tool counterfactual
        have it."""
        blob = self._blob(value) if value is not None else None
        entry = {"surface": surface, "etag": etag, "version": version, "receipt_id": receipt_id, "blob": blob}
        with self._lock:
            if not self.closed:
                self.reads.append({k: v for k, v in entry.items() if v is not None})

    def observe_state(self, values: Iterable[StateValue]) -> None:
        """Fields of objects a read served the turn, kept for the claim check."""
        with self._lock:
            if not self.closed:
                self.state.extend(values)

    def pack(self, *, compiler: str | None, pack_hash: str | None) -> None:
        """The pack this turn read: its compiler's version and hash become the turn's `niadra` pins."""
        pins = {k: v for k, v in (("compiler", compiler), ("pack_hash", pack_hash)) if v}
        if pins:
            with self._lock:
                self.pins["niadra"] = pins

    def say(
        self,
        text: str,
        *,
        event_key: str | None = None,
        context: str = "chat",
        immutable: bool = False,
        agent: str | None = None,
    ) -> None:
        """Something the turn emitted: `event_key` names the event `track()` sent for it (the text itself is
        never repeated in the record), and the text is kept for the claim check the sender runs."""
        with self._lock:
            if self.closed:
                return
            if event_key is not None:
                self.event_keys.append(event_key)
            if self._recorder is not None and text not in self._guarded:
                self.said.append(Said(text, context, immutable, agent or self.agent))

    def interact(self, item: Mapping[str, Any] | BaseModel) -> None:
        """What the person was shown or did in the turn, as the interaction spec writes it: a `presented` list
        (with its `exposure_id` and positions), a `seen`, an `engaged` item of it, a preference. A replay and
        the tool counterfactual read where the person engaged from here."""
        entry = json.loads(snapshot(item))  # a copy, now, as JSON
        with self._lock:
            if not self.closed:
                self.interactions.append(entry)

    def coordinate(self, decision_id: str, decision: str) -> None:
        """A coordination decision the turn acted on."""
        with self._lock:
            if not self.closed:
                self.coordination.append({"decision_id": decision_id, "decision": decision})

    def effect(self, key: str, state: str) -> None:
        """What the turn saw of an effect: its last state by key."""
        with self._lock:
            if not self.closed:
                self.effects[key] = state

    def guarded(self, text: str) -> None:
        """The claim guard checked `text` and recorded its claims: saying it later adds none."""
        with self._lock:
            self._guarded.add(text)

    def flag(self, name: Flag) -> None:
        """Marks the turn: a flagged turn keeps its values longest in the queue and goes to the kept tier."""
        with self._lock:
            self.flags.add(name)

    def incomplete(self) -> None:
        """The capture failed during the turn: its record says so."""
        with self._lock:
            self.completeness = "incomplete"
            self.flags.add("incomplete")

    # Lifecycle

    def open(self) -> TurnFrame:
        """Makes this the current turn of the running task or thread. The `with` block does it for you."""
        self._token = _turn.set(self)
        return self

    def close(self, error: BaseException | None = None) -> None:
        """Ends the turn and hands its record to the queue. Later calls do nothing."""
        _reset(_turn, self._token)
        self._token = None
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self.ended_at = datetime.now(timezone.utc)
            self.latency_ms = int((self.ended_at - self.started_at).total_seconds() * 1000)
            if error is not None:
                self.flags.add("error")
            for entry in self.calls:
                if "status" not in entry:  # a call the turn left open never returned in it
                    entry["status"] = "cancelled"
        if self._recorder is not None and self.playback is None:
            self._recorder.submit(self)

    def __enter__(self) -> TurnFrame:
        return self.open()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close(exc)

    async def __aenter__(self) -> TurnFrame:
        return self.open()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close(exc)

    # For the queue and the sender

    def calls_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self.calls]

    def value_of(self, key: str | None) -> Any:
        """The value of a blob the frame still holds, read back from its copy; None otherwise."""
        blob = self.blobs.get(key) if key else None
        data = blob.data if blob is not None else None
        if data is None:
            return None
        try:
            return json.loads(data, parse_constant=lambda _: None)
        except ValueError:
            return None

    def add_claims(self, records: Iterable[dict[str, Any]]) -> None:
        with self._lock:
            self.claims.extend(records)

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    @property
    def size(self) -> int:
        """Bytes the queue counts for this turn: its blobs' values and an estimate of the frame."""
        return self.size_without_values + sum(b.size for b in self.blobs.values() if b.data is not None)

    @property
    def size_without_values(self) -> int:
        return FRAME_BYTES + CALL_BYTES * len(self.calls)

    def drop_blobs(self) -> int:
        """Drops the values of the blobs to make room. A blob whose digest is known stays as its digest;
        the others leave the record, with the call fields that named them. Returns the bytes freed."""
        freed = 0
        with self._lock:
            for key, blob in list(self.blobs.items()):
                if blob.data is None:
                    continue
                freed += blob.size
                blob.data = None
                if blob.sha256 is None:
                    del self.blobs[key]
                    for entry in self.calls:
                        for name in ("args", "result_model", "result_ui"):
                            if entry.get(name) == key:
                                del entry[name]
            if freed and self.completeness == "complete":
                self.completeness = "partial"
        return freed

    def _next(self, prefix: str) -> int:
        with self._lock:
            self._counters[prefix] += 1
            return self._counters[prefix]

    def _blob(self, value: Any) -> str | None:
        try:
            data = snapshot(value)
        except Exception:
            logger.debug("niadra: a value of turn %s could not be copied", self.turn_id, exc_info=True)
            self.incomplete()
            return None
        key = f"b:{self._next('b')}"
        with self._lock:
            self.blobs[key] = Blob(data, len(data))
        return key

    def _add(self, entry: dict[str, Any]) -> CallCapture:
        with self._lock:
            if not self.closed:
                self.calls.append(entry)
        return CallCapture(self, entry)
