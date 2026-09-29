"""`client.turns`: opens turns, keeps the closed ones in the bounded queue, and holds what the sender needs.

Recording is on as long as the client has a key and the space records turns: a `404` on `POST /v1/turns`
means it does not, and the recorder then stops recording for 10 minutes before it tries again, the way
`prefetch()` treats a server without its route. While it is off, a turn still opens and closes (the agent's
code runs the same), and its record is not kept.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from niadra.models.turns import TurnPins
from niadra.options import TurnOptions
from niadra.turns.capture import TurnFrame, TurnKind, current_turn
from niadra.turns.queue import TurnQueue
from niadra.turns.record import ContentMode
from niadra.turns.sender import MAX_TURNS
from niadra.turns.store import BlobStore, S3Store

if TYPE_CHECKING:
    from niadra.turns.sender import AsyncTurnSender, SyncTurnSender

logger = logging.getLogger("niadra")

OFF_FOR = 600.0
"""Seconds a recorder stays off after the space answered that it does not record turns."""

PinsLike = TurnPins | Mapping[str, Any]


class TurnRecorder:
    """The turns of one client. See `open()`, `store()`, and the counters."""

    def __init__(self, options: TurnOptions, *, enabled: bool) -> None:
        self.options = options
        self.interval = options.interval
        self.queue = TurnQueue(options.max_bytes, options.max_turns, options.interval, MAX_TURNS)
        self.sender: SyncTurnSender | AsyncTurnSender | None = None
        self.blob_store: BlobStore | None = None
        self.claims: Callable[[TurnFrame], list[dict[str, Any]]] | None = None
        """The claim check the sender runs on a turn's outputs, when a claim contract applies."""
        self.recording_mode: Callable[[], str | None] = lambda: None
        """The mode the space's recording names, when the client knows it (the SDK profile)."""
        self.required_pins: Callable[[], tuple[str, ...]] = lambda: ()
        """The pins a turn needs to be replayable, when the client knows them (the SDK profile)."""
        self.features: Callable[[], frozenset[str] | None] = lambda: None
        """The features the space turned on, when the client knows them (the SDK profile)."""
        self._enabled = enabled
        self._off_until = 0.0
        self._refused = False
        self._lock = threading.Lock()
        self._warned: set[str] = set()
        self.accepted = 0
        self.duplicates = 0
        self.rejected_turns = 0
        self.not_kept = 0

    # The agent's side

    def open(
        self,
        *,
        agent: str | None = None,
        role: str | None = None,
        kind: TurnKind = "message",
        build: PinsLike | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        turn_id: str | None = None,
        adapter: str | None = None,
    ) -> TurnFrame:
        """A new turn, not yet current: use it as a context manager (`with`, or `async with`), or call its
        `open()` and `close()`.

        Opened while another turn is current, it is a sub-turn of it, in its conversation unless one is
        named. Without ids it belongs to the conversation or task whose block is running
        (`current_session()`); `agent` defaults to that session's `agent_id`. `build` pins what a replay
        needs (`Niadra.build()`)."""
        from niadra.conversation import current_session  # the conversation module imports this one

        parent = current_turn()
        session = current_session()
        if conversation_id is None and task_id is None and parent is None and session is not None:
            conversation_id, task_id = session.conversation_id, session.task_id
        if agent is None:
            agent = (session.agent_id if session is not None else None) or (
                parent.agent if parent else "agent"
            )
        return TurnFrame(
            self if self.recording else None,
            agent=agent,
            role=role,
            kind=kind,
            conversation_id=conversation_id,
            task_id=task_id,
            pins=_pins(build),
            adapter=adapter,
            turn_id=turn_id,
            parent=parent,
        )

    def submit(self, frame: TurnFrame) -> None:
        """Takes a closed turn into the queue. Never waits and never raises."""
        if frame.conversation_id is None and frame.task_id is None:
            self._warn_once("no_session", "niadra: a turn outside a conversation or a task is not recorded")
            return
        if not self.recording:
            return
        missing = [pin for pin in self.required_pins() if not frame.pins.get(pin)]
        if missing:
            names = ", ".join(missing)
            self._warn_once(
                f"pins:{names}",
                f"niadra: turns without the {names} pin are kept but cannot be replayed; "
                "name them in Niadra.build()",
            )
        try:
            self.queue.put(frame)
            if self.sender is not None:
                self.sender.notify()
        except Exception:
            logger.warning("niadra: a closed turn could not be queued", exc_info=True)

    # The company's side

    def store(
        self, put: BlobStore | None = None, *, s3_bucket: str | None = None, client: Any = None
    ) -> None:
        """Keeps recorded values in the company's storage (`pointer` mode): Niadra receives only pointers and
        digests. Pass a callable `put(key, data) -> pointer`, or `s3_bucket="s3://bucket/prefix"` to write
        with boto3 (`client` is a boto3 S3 client; by default one from the environment)."""
        if (put is None) == (s3_bucket is None):
            raise ValueError("pass a store callable or s3_bucket=, one of them")
        self.blob_store = put if put is not None else S3Store(s3_bucket or "", client=client)

    @property
    def content_mode(self) -> ContentMode:
        """Where recorded values go: `content_mode` of `TurnOptions`; else `pointer` with a store; else the
        space's recording mode; else `stored`. After the server refused a mode, digests only."""
        if self._refused:
            return "hash_only"
        if self.options.content_mode is not None:
            return self.options.content_mode
        if self.blob_store is not None:
            return "pointer"
        mode = self.recording_mode()
        if mode == "pointer":
            return "pointer"
        return "hash_only" if mode == "hash_only" else "stored"

    def mode_of(self, frame: TurnFrame) -> ContentMode:
        return frame.mode or self.content_mode

    @property
    def recording(self) -> bool:
        """Whether closed turns are kept: the client has a key, and the space records turns as far as the
        SDK knows (the profile lists `turns`, or it has not been read yet)."""
        features = self.features()
        on = features is None or "turns" in features
        return self._enabled and on and time.monotonic() >= self._off_until

    @property
    def pending(self) -> int:
        """Closed turns waiting in the queue."""
        return len(self.queue)

    @property
    def dropped(self) -> int:
        """Turns lost so far: dropped whole by a full queue, refused by the API, or not kept by the space."""
        return self.queue.turns_dropped + self.rejected_turns + self.not_kept

    # What the sender reports

    def sent(self, accepted: int, duplicates: int) -> None:
        with self._lock:
            self.accepted += accepted
            self.duplicates += duplicates

    def rejected(self, count: int, codes: set[str]) -> None:
        with self._lock:
            self.rejected_turns += count
        logger.warning("niadra: %d turn records were refused (%s)", count, ", ".join(sorted(codes)))

    def not_recorded(self, count: int, *, off: bool) -> None:
        """The space does not record turns: these are dropped, and with `off` (the route answered 404)
        recording stops for a while."""
        with self._lock:
            self.not_kept += count
            if off:
                self._off_until = time.monotonic() + OFF_FOR
        self._warn_once("off", "niadra: this space does not record turns; turn recording is off for now")

    def mode_refused(self) -> None:
        self._refused = True
        self._warn_once(
            "refused",
            "niadra: the source records less than this SDK sent; turns now leave with digests only "
            "(set TurnOptions.content_mode, or turns.store() for pointer mode)",
        )

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message)


def _pins(build: PinsLike | None) -> dict[str, Any]:
    if build is None:
        return {}
    if isinstance(build, BaseModel):
        return build.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
    return TurnPins.model_validate(build).model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude_defaults=True
    )
