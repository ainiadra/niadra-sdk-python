"""The bounded turn queue: closed turns wait here for the sender, apart from the event queue.

It is bounded twice, by bytes and by turns (`TurnOptions`). `put` never waits and never does I/O. When a
turn does not fit, the queue makes room in this order:

1. it drops the values of turns that carry no flag, oldest first. Each such turn keeps its frame, and its
   blobs keep their keys: the record leaves with digests only, `completeness: partial`;
2. only then does it drop the oldest turns whole.

A flagged turn (an error, a guard that acted, a handoff, a synthetic call...) keeps its values longest,
because it is the one someone will replay. Both kinds of loss are counted, and logged at most once a minute.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from niadra.turns.capture import TurnFrame

logger = logging.getLogger("niadra")

LOG_EVERY = 60.0


class TurnQueue:
    def __init__(self, max_bytes: int, max_turns: int, interval: float, batch: int) -> None:
        self._max_bytes = max_bytes
        self._max_turns = max_turns
        self._interval = interval
        self._batch = batch
        self._turns: deque[TurnFrame] = deque()
        # Unflagged turns whose values may still go, oldest first; a turn that left is skipped lazily.
        self._sheddable: deque[TurnFrame] = deque()
        self._sizes: dict[int, int] = {}
        self._bytes = 0
        self._first_at: float | None = None
        self._lock = threading.Lock()
        self._logged_at = 0.0
        self.values_dropped = 0
        """Turns that left with digests only, to make room."""
        self.turns_dropped = 0
        """Turns dropped whole: the queue was full of flagged turns, or of frames alone."""

    def put(self, frame: TurnFrame) -> None:
        with self._lock:
            size = frame.size
            self._turns.append(frame)
            self._sizes[id(frame)] = size
            self._bytes += size
            if _sheddable(frame):
                self._sheddable.append(frame)
            if self._first_at is None:
                self._first_at = time.monotonic()
            shed = self._make_room()
        if shed:
            self._log()

    def requeue(self, frames: list[TurnFrame]) -> None:
        """Puts turns the sender could not deliver back at the front, keeping only what fits."""
        with self._lock:
            for frame in reversed(frames):
                size = frame.size
                self._turns.appendleft(frame)
                self._sizes[id(frame)] = size
                self._bytes += size
                if _sheddable(frame):
                    self._sheddable.appendleft(frame)
            if self._turns and self._first_at is None:
                self._first_at = time.monotonic()
            shed = self._make_room()
        if shed:
            self._log()

    def take(self, limit: int) -> list[TurnFrame]:
        with self._lock:
            taken = [self._turns.popleft() for _ in range(min(limit, len(self._turns)))]
            for frame in taken:
                self._bytes -= self._sizes.pop(id(frame), 0)
            while self._sheddable and id(self._sheddable[0]) not in self._sizes:
                self._sheddable.popleft()
            self._first_at = time.monotonic() if self._turns else None
            return taken

    def next_due(self) -> float | None:
        """When what is waiting should leave: at once with a full batch, else `interval` after the first."""
        with self._lock:
            if not self._turns or self._first_at is None:
                return None
            if len(self._turns) >= self._batch:
                return self._first_at
            return self._first_at + self._interval

    def __len__(self) -> int:
        with self._lock:
            return len(self._turns)

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes

    def _make_room(self) -> bool:
        shed = False
        # Values make room for bytes only: past the count of turns, only whole turns can go.
        while self._bytes > self._max_bytes and self._sheddable:
            frame = self._sheddable.popleft()
            if id(frame) not in self._sizes:
                continue  # it left already
            frame.drop_blobs()
            size = frame.size
            self._bytes += size - self._sizes[id(frame)]
            self._sizes[id(frame)] = size
            self.values_dropped += 1
            shed = True
        while self._over() and self._turns:
            frame = self._turns.popleft()
            self._bytes -= self._sizes.pop(id(frame), 0)
            self.turns_dropped += 1
            shed = True
        return shed

    def _over(self) -> bool:
        return self._bytes > self._max_bytes or len(self._turns) > self._max_turns

    def _log(self) -> None:
        now = time.monotonic()
        if now - self._logged_at < LOG_EVERY:
            return
        self._logged_at = now
        logger.warning(
            "niadra: the turn queue is full: %d turns left with digests only and %d were dropped so far",
            self.values_dropped,
            self.turns_dropped,
        )


def _sheddable(frame: TurnFrame) -> bool:
    return not frame.flagged and frame.size > frame.size_without_values
