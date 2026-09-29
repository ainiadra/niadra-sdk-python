"""The customer's turn on the read path: sent as `query`, and prefetched while they speak.

A conversation sends the customer's last turn as `query` on every `context()`. The server keeps
serving the conversation's pinned pack and adds `slots`, what that turn selected from memory, so
the SDK caches the pack as a read without `query` and never caches the slots.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

MAX_TURN = 2000
# A partial transcript shorter than this says nothing the server can use yet.
MIN_PREFETCH = 8
# Statuses of a server without the prefetch route, and how long, in seconds, not to ask it again.
NO_PREFETCH = frozenset({404, 405, 501})
PREFETCH_RECHECK_AFTER = 600.0


def turn_text(text: str | None) -> str | None:
    """The turn as `query`: trimmed, and its last `MAX_TURN` characters when longer. None when blank."""
    if text is None:
        return None
    stripped = text.strip()
    return stripped[-MAX_TURN:] if stripped else None


class PrefetchSupport:
    """Whether this client's server answers prefetches: a route it lacks is not asked for a while."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._prefetch_at = 0.0

    def prefetch_wanted(self) -> bool:
        with self._lock:
            return self._clock() >= self._prefetch_at

    def prefetch_refused(self) -> None:
        """The server has no prefetch route: stop sending for `PREFETCH_RECHECK_AFTER` seconds."""
        with self._lock:
            self._prefetch_at = self._clock() + PREFETCH_RECHECK_AFTER


class BlockSupport:
    """Which blocks of `include` this client's space serves: a block it refused is not asked for a while,
    and the read goes on without it."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._refused: dict[str, float] = {}

    def wanted(self, names: Iterable[str]) -> list[str]:
        """`names`, in order and once each, without those refused less than `PREFETCH_RECHECK_AFTER` ago."""
        now = self._clock()
        with self._lock:
            return [n for n in dict.fromkeys(names) if self._refused.get(n, 0.0) <= now]

    def refused(self, names: Iterable[str]) -> None:
        with self._lock:
            for name in names:
                self._refused[name] = self._clock() + PREFETCH_RECHECK_AFTER
