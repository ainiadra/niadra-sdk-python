"""`niadra resolver-worker`: the space's refresh requests, served inside the company's boundary.

Niadra never calls a company system. When a value must be read again (a claim waits on it, a timer is due,
someone watches the object), Niadra queues a refresh request with the budget it admitted. The worker leases
the waiting requests (`GET /v1/state/refresh-requests`, 60 s), reads each object with the company's resolver
of its type (`niadra.resolvers`), at the resolver's rate and behind its circuit breaker, and pushes what it
read (`POST /v1/objects/push`) with the request's id, which settles it.

A watch fires only on a value its source confirmed: its `watch_revalidation` requests go first, and the answer
decides every due watch of the object, even when the value did not change. A request the worker cannot
answer, the object gone from the source (`NOT_FOUND`) or the resolver failing, is released at once
(`POST .../release`), so a watch falls back to its type's rule without waiting out the leases. A request of a
type with no resolver, or whose resolver's circuit is open, is left to its lease, for a worker that can read
it. A worker that leases regularly is what tells Niadra a company worker is there to revalidate.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from niadra._transport import Request
from niadra.errors import NiadraError
from niadra.models.state import RefreshRequest, RefreshRequestPage
from niadra.resolvers import Resolved, push_item

if TYPE_CHECKING:
    from niadra._client import Niadra

logger = logging.getLogger("niadra")

PUSH_MAX = 1000
LEASE = 60.0
WATCH = "watch_revalidation"
PRIORITY = {"high": 0, "normal": 1, "low": 2}


class ResolverWorker:
    """Serves one space's refresh requests with `niadra.resolvers`. `budget` bounds each resolver call."""

    def __init__(self, niadra: Niadra, *, limit: int = 50, poll: float = 2.0, budget: float = 5.0) -> None:
        self._niadra = niadra
        self._limit = limit
        self._poll = poll
        self._budget = budget
        self._missing: set[str] = set()
        self.pushed = 0
        self.skipped = 0
        self.released = 0

    def run_once(self) -> int:
        """Leases the waiting requests, resolves them and pushes what was read. Returns how many objects
        were pushed."""
        transport = self._niadra._transport
        leased_at = time.monotonic()
        page = RefreshRequestPage.model_validate(
            transport.request(Request("GET", "/v1/state/refresh-requests", params={"limit": self._limit}))
        )
        resolvers = self._niadra.resolvers
        objects: list[dict[str, Any]] = []
        for request in sorted(page.items, key=_order):
            kind = request.ref.type
            if kind not in resolvers:
                if kind not in self._missing:
                    self._missing.add(kind)
                    logger.warning("niadra: no resolver for objects of type %s; their requests wait", kind)
                self.skipped += 1
                continue
            if not resolvers.available(kind):
                self.skipped += 1  # the circuit is open: the lease brings it back
                continue
            wait = resolvers.wait(kind)
            if time.monotonic() + wait - leased_at >= LEASE:
                self.skipped += 1  # past the lease: another worker may take it
                continue
            time.sleep(wait)
            found = resolvers.fetch(request.ref, None, self._budget)
            if isinstance(found, Resolved):
                objects.append({**push_item(request.ref, found), "request_id": request.request_id})
            else:
                self._release(request, found)
        for start in range(0, len(objects), PUSH_MAX):
            body = {"objects": objects[start : start + PUSH_MAX]}
            transport.request(Request("POST", "/v1/objects/push", json=body))
        self.pushed += len(objects)
        return len(objects)

    def _release(self, request: RefreshRequest, outcome: str) -> None:
        """Gives back a request the worker cannot answer; one that failed to go back waits out its lease."""
        try:
            self._niadra._transport.request(
                Request(
                    "POST",
                    f"/v1/state/refresh-requests/{quote(request.request_id, safe='')}/release",
                    json={"outcome": outcome},
                )
            )
            self.released += 1
        except NiadraError as exc:
            logger.warning("niadra: a refresh request could not be released (%s)", type(exc).__name__)
            self.skipped += 1

    def run(self, stop: threading.Event | None = None) -> None:
        """Serves requests until `stop` is set, pausing `poll` seconds when none wait, and longer while Niadra
        does not answer."""
        stop = stop or threading.Event()
        pause = self._poll
        while not stop.is_set():
            try:
                done = self.run_once()
                pause = self._poll
            except Exception as exc:
                logger.warning("niadra: the resolver worker could not reach Niadra (%s)", type(exc).__name__)
                done, pause = 0, min(pause * 2, 60.0)
            if not done:
                stop.wait(pause)


def _order(request: RefreshRequest) -> tuple[bool, int]:
    """Watches first, since a customer waits on them; then by the priority Niadra gave."""
    return (request.reason != WATCH, PRIORITY.get(request.priority, 1))
