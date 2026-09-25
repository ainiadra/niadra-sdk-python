"""The customer's turn on the read path (memory v2): sent as `query`, and prefetched while they speak.

A conversation sends the customer's last turn as `query` on every `context()`. In a space with
memory v2 the server keeps serving the conversation's pinned pack and adds `slots`, what that turn
selected from memory, so the SDK caches the pack as a read without `query` and never caches the
slots. A space without memory v2 compiles a read with `query` for that query and does not pin it,
so there the turn is not sent: the SDK learns which kind of space it talks to from the answers,
per client (a key belongs to one space), and asks again after `RECHECK_AFTER` seconds, in case the
space turned memory v2 on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from niadra.models.context import ContextResponse

RECHECK_AFTER = 600.0
# The least budget, in seconds, worth a second read when a space answered the turn without slots.
MIN_REREAD = 0.02
MAX_TURN = 2000
# A partial transcript shorter than this says nothing the server can use yet.
MIN_PREFETCH = 8
# Statuses of a server without the prefetch route.
NO_PREFETCH = frozenset({404, 405, 501})


def reads_turn(response: ContextResponse) -> bool:
    """Whether the server read the turn for slots: it sends `slots`, or times the stage that picks
    them, which it runs on every read with `query` in a space with memory v2 that has a pack."""
    return response.slots is not None or "slots" in response.timing


def turn_text(text: str | None) -> str | None:
    """The turn as `query`: trimmed, and its last `MAX_TURN` characters when longer. None when blank."""
    if text is None:
        return None
    stripped = text.strip()
    return stripped[-MAX_TURN:] if stripped else None


class TurnSupport:
    """What this client learned about its space: whether it reads the turn, and has the prefetch route."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._reads: bool | None = None
        self._recheck_at = 0.0
        self._prefetch_at = 0.0

    @property
    def reads_turn(self) -> bool | None:
        """True or False once an answer said so; None before that."""
        return self._reads

    def wanted(self) -> bool:
        """Whether to send the turn: unless the space answered without slots less than `RECHECK_AFTER` ago."""
        with self._lock:
            return self._reads is not False or self._clock() >= self._recheck_at

    def observe(self, response: ContextResponse) -> bool | None:
        """Learns from an answer to a read that sent the turn. None when the answer cannot tell (no pack)."""
        if reads_turn(response):
            with self._lock:
                self._reads = True
            return True
        if not (response.text or response.not_modified):
            return None
        with self._lock:
            self._reads = False
            self._recheck_at = self._clock() + RECHECK_AFTER
        return False

    def prefetch_wanted(self) -> bool:
        with self._lock:
            return self._reads is not False and self._clock() >= self._prefetch_at

    def prefetch_refused(self) -> None:
        """The server has no prefetch route: stop sending for `RECHECK_AFTER` seconds."""
        with self._lock:
            self._prefetch_at = self._clock() + RECHECK_AFTER
