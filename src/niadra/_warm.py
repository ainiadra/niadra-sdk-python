"""Keeps the connection to the region open while a conversation is in use (B23).

The client closes an idle connection after `KEEPALIVE_S` (120 s, under the 180 s the API's edge keeps one),
and chat turns are often further apart than that: the next read then opens a connection again, TCP and TLS
first, two more round trips (330 ms from Sao Paulo), and a DNS lookup whenever the resolver's copy expired
(about 300 ms more). While a conversation is open (created and not ended) and was used in the last
`WARM_FOR_S`, the client sends `GET /healthz` every `EVERY_S` when nothing else went out for `IDLE_S`: one
attempt, a 2-second budget, on the same connection pool, and never counted as the client's own use. The
API answers it in a fraction of a millisecond without touching a database.
"""

from __future__ import annotations

import threading
import time
import weakref
from typing import Any, Literal

from niadra._transport import Request

EVERY_S = 100.0
"""Under the 120 s the client keeps an idle connection and the 180 s the edge keeps one."""
IDLE_S = 90.0
"""A ping goes only when nothing else went out for this long."""
WARM_FOR_S = 600.0
"""How long after the client's last use a conversation still left open keeps the connection warm: a
conversation never ended does not ping forever."""


def ping_http() -> Request:
    return Request("GET", "/healthz", timeout=2.0, budget=2.0, max_attempts=1, activity=False)


class KeepWarm:
    """The open conversations of a client, and whether its timer should ping, wait or stop."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._sessions: weakref.WeakSet[Any] = weakref.WeakSet()
        self._lock = threading.Lock()
        self._running = False
        self._opened_at = 0.0

    def add(self, session: Any) -> bool:
        """Notes an open conversation; True when the caller should start the timer."""
        if not self.enabled:
            return False
        with self._lock:
            self._sessions.add(session)
            self._opened_at = time.monotonic()
            if self._running:
                return False
            self._running = True
            return True

    def step(self, now: float, last_activity: float | None) -> Literal["ping", "wait", "stop"]:
        with self._lock:
            used = max(last_activity or 0.0, self._opened_at)
            still_open = any(not getattr(s, "_ended", False) for s in list(self._sessions))
            if not still_open or now - used > WARM_FOR_S:
                self._running = False
                return "stop"
        return "ping" if now - used >= IDLE_S else "wait"
