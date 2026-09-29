"""The local copy of the suppression list (`spec/suppression-list.md`): the opt-out holds with Niadra down.

The copy is read by cursor from `GET /v1/suppressions`, with this reader's salt from
`GET /v1/suppressions/salt`, and read again once it is `REFRESH` seconds old. Before an outbound contact the
SDK computes the destination's key and looks it up here, in memory:

- an entry with that key, the purpose, the channel (or every channel), in force now, forbids the contact;
- when the list cannot be read, the last copy keeps applying, however old: an opt-out does not wait for
  Niadra;
- with no copy at all and Niadra out of reach, the purpose decides: transactional and service contacts go
  (they fail open), marketing, retention and collection wait (they fail closed);
- a space that keeps no list (the route answers 404) has nothing to suppress.

The salt and the keys never leave the process.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from niadra._transport import Request
from niadra.coordination.destination import DestinationError, canonical_destination, suppression_key
from niadra.errors import NotFoundError
from niadra.models.coordination import Suppression, SuppressionPage, SuppressionSalt

REFRESH = 60.0
"""Seconds after which the copy is read again."""
FAIL_OPEN = frozenset({"transactional", "service"})
"""Purposes whose contacts go when Niadra cannot say; every other purpose waits."""
PAGE = 200
MAX_PAGES = 50
"""Pages one read takes at most; the next read goes on from its cursor."""


class SuppressionCopy:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._salt: SuppressionSalt | None = None
        self._entries: dict[str, Suppression] = {}
        self._cursor: str | None = None
        self._read_at: float | None = None
        self._absent = False
        """The space keeps no list: nothing is suppressed."""

    # The reads

    def due(self) -> bool:
        with self._lock:
            return self._read_at is None or self._clock() - self._read_at >= REFRESH

    @property
    def held(self) -> bool:
        """Whether the SDK has a copy (or knows there is no list to copy)."""
        with self._lock:
            return self._absent or self._read_at is not None

    @staticmethod
    def salt_request(timeout: float) -> Request:
        return Request("GET", "/v1/suppressions/salt", timeout=timeout, budget=timeout)

    def page_request(self, timeout: float) -> Request:
        params: dict[str, Any] = {"limit": PAGE}
        with self._lock:
            if self._cursor is not None:
                params["cursor"] = self._cursor
        return Request("GET", "/v1/suppressions", params=params, timeout=timeout, budget=timeout)

    def needs_salt(self) -> bool:
        with self._lock:
            return self._salt is None

    def take_salt(self, data: Any) -> None:
        salt = SuppressionSalt.model_validate(data)
        with self._lock:
            if self._salt is not None and self._salt.salt_id != salt.salt_id:
                self._entries.clear()  # keys of another salt name no one now
                self._cursor = None
            self._salt = salt

    def apply(self, data: Any) -> bool:
        """Applies one page, in order. True when more pages follow."""
        page = SuppressionPage.model_validate(data)
        with self._lock:
            if self._salt is None or page.salt_id != self._salt.salt_id:
                # The salt changed: the keys held name no one now. Take the new salt, then read it all.
                self._salt, self._cursor = None, None
                self._entries.clear()
                return True
            if page.reset:
                self._entries.clear()
            for entry in page.items:
                if entry.removed:
                    self._entries.pop(entry.id, None)
                else:
                    self._entries[entry.id] = entry
            self._cursor = page.next_cursor or self._cursor
            self._absent = False
            if not page.next_cursor:
                self._read_at = self._clock()
            return bool(page.next_cursor)

    def failed(self, error: BaseException) -> None:
        """A 404: the space keeps no list. Anything else: the copy stays as it is."""
        if isinstance(error, NotFoundError):
            with self._lock:
                self._absent, self._read_at = True, self._clock()
                self._entries.clear()

    # The check

    def may_contact(
        self,
        handle: Mapping[str, Any],
        purpose: str,
        *,
        channel: str | None = None,
        now: datetime | None = None,
        fail_open: bool | None = None,
    ) -> bool:
        """Whether an outbound contact of `purpose` to `handle` may go, by the local copy. `fail_open`
        overrides the purpose's direction when there is no copy."""
        with self._lock:
            if self._absent:
                return True
            salt, entries, held = self._salt, list(self._entries.values()), self._read_at is not None
        if salt is None or not held:
            return fail_open if fail_open is not None else purpose in FAIL_OPEN
        try:
            canonical = canonical_destination(str(handle["type"]), str(handle["value"]))
            key = suppression_key(salt.salt, canonical)
        except DestinationError:
            return True  # not a destination the list covers
        moment = now or datetime.now(timezone.utc)
        return not any(
            e.key == key
            and e.purpose == purpose
            and (e.channel is None or e.channel == channel)
            and e.since <= moment
            and (e.until is None or e.until > moment)
            for e in entries
        )
