"""The local copy of the suppression list (`spec/suppression-list.md`): the opt-out holds with Niadra down.

The copy is read by cursor from `GET /v1/suppressions` (a page shorter than the limit is the end of the
changes for now, and its cursor is where the next read starts), with this reader's salt from
`GET /v1/suppressions/salt`, and read again once it is `REFRESH` seconds old. Before an outbound contact the
SDK computes the destination's key and looks it up here, in memory:

- an entry with that key, the purpose (or `any`), the channel (or every channel), in force now, forbids the
  contact; one with a `window` (the person's own contact hours, section 6.4) forbids it only inside those
  local hours, and the contact waits until the window ends;
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
from niadra.coordination.window import InvalidWindowError, window_until
from niadra.errors import NotFoundError
from niadra.models.coordination import Suppression, SuppressionPage, SuppressionSalt

ANY_PURPOSE = "any"
"""An entry of `any` purpose holds for every purpose: a person's own contact hours."""
REFRESH = 60.0
"""Seconds after which the copy is read again."""
FAIL_OPEN = frozenset({"transactional", "service"})
"""Purposes whose contacts go when Niadra cannot say; every other purpose waits."""
PAGE = 200
MAX_PAGES = 50
"""Pages one read takes at most; the next read goes on from its cursor."""
FIRST_READ_ROUNDS = 2
"""Round trips the first check waits for, each within its own budget: the salt and the first page, the
whole list of a space with up to `PAGE` entries. One budget for both ran out from Sao Paulo on a new
client (09/10/2026: 0.6 s for about 1.1 s of round trips), and every first check of a purpose that fails
closed answered no, whatever the channel. A longer list goes on in the background."""


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
        """Applies one page, in order. True when more pages follow: a full page. Every page carries the cursor
        to read from next, the last one too, which the next read starts from."""
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
            more = bool(page.next_cursor) and len(page.items) >= PAGE
            if not more:
                self._read_at = self._clock()
            return more

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
        """Whether an outbound contact of `purpose` to `handle` may go at `now`, by the local copy.
        `fail_open` overrides the purpose's direction when there is no copy."""
        found = self.blocking(handle, purpose, channel=channel, now=now)
        if found is None:
            return fail_open if fail_open is not None else purpose in FAIL_OPEN
        return not found.suppressed and found.window_until is None

    def blocking(
        self,
        handle: Mapping[str, Any],
        purpose: str,
        *,
        channel: str | None = None,
        now: datetime | None = None,
    ) -> Blocking | None:
        """What the copy says of a contact at `now`: suppressed at every hour, or inside a contact window
        until when; None when there is no copy to say it."""
        with self._lock:
            if self._absent:
                return Blocking()
            salt, entries, held = self._salt, list(self._entries.values()), self._read_at is not None
        if salt is None or not held:
            return None
        try:
            canonical = canonical_destination(str(handle["type"]), str(handle["value"]))
            key = suppression_key(salt.salt, canonical)
        except DestinationError:
            return Blocking()  # not a destination the list covers
        moment = now or datetime.now(timezone.utc)
        live = [
            e
            for e in entries
            if e.key == key
            and e.purpose in (purpose, ANY_PURPOSE)
            and (e.channel is None or e.channel == channel)
            and e.since <= moment
            and (e.until is None or e.until > moment)
        ]
        ends = [end for e in live if e.window is not None and (end := _window_end(e, moment)) is not None]
        return Blocking(any(e.window is None for e in live), max(ends) if ends else None)


class Blocking:
    """What the local copy says of one contact: `suppressed` at every hour, or inside a contact window until
    `window_until`."""

    __slots__ = ("suppressed", "window_until")

    def __init__(self, suppressed: bool = False, window_until: datetime | None = None) -> None:
        self.suppressed = suppressed
        self.window_until = window_until


def _window_end(entry: Suppression, at: datetime) -> datetime | None:
    """When the entry's window `at` falls in ends. A window this machine cannot read (its time zone database
    lacks the zone) is not applied, and is never taken as every hour (section 6.5)."""
    try:
        return window_until(entry.window, at)
    except InvalidWindowError:
        return None
