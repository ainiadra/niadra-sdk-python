"""The per-conversation context cache: TTL, stale-while-revalidate and last good value.

Every agent framework rebuilds the prompt on each turn and none of them caches the context
call, so the SDK does. Only calls that carry a `conversation_id` or `task_id` are cached,
because only those are pinned by the server to the same bytes.

A delta is what changed since this agent last read the subject, and the server moves that
mark as it answers, so a delta it sent once never comes back. The cache therefore hands each
delta out exactly once: one fetched by a background refresh waits in the entry until the next
read takes it, instead of being overwritten or served twice.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

from niadra.errors import AuthenticationError, PermissionDeniedError
from niadra.models.context import ContextRequest
from niadra.models.results import Context
from niadra.options import CacheOptions

Freshness = Literal["fresh", "stale", "expired"]

_OUTSIDE_THE_PACK = (
    "coverage",
    "verification",
    "withheld",
    "live",
    "live_complete",
    "timing",
    "path",
    "elapsed_ms",
)


def cache_key(request: ContextRequest) -> str:
    """A digest of everything that selects a pack. Handles never sit in memory as dict keys.

    `delta` is left out: a plain read and a delta read of one conversation get the same pinned
    bytes, so they share one entry.
    """
    payload = request.model_dump(mode="json", exclude={"known_etag", "delta"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class _Entry:
    context: Context
    stored_at: float
    scope: str
    deltas: list[str] = field(default_factory=list)

    def hand_out(self, context: Context) -> Context:
        """`context` carrying the deltas not yet delivered, which then count as delivered."""
        delta = "\n\n".join(self.deltas) or None
        self.deltas = []
        return context.model_copy(update={"delta": delta})


@dataclass(frozen=True)
class Hit:
    context: Context
    freshness: Freshness


class ContextCache:
    def __init__(self, options: CacheOptions) -> None:
        self._options = options
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._refreshing: set[str] = set()
        self._lock = threading.Lock()

    def get(self, key: str) -> Hit | None:
        """The cached pack for `key`. A fresh or stale hit also delivers the pending deltas."""
        with self._lock:
            entry = self._live(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            age = time.monotonic() - entry.stored_at
            if age < self._options.ttl:
                return Hit(entry.hand_out(entry.context.model_copy(update={"origin": "cache"})), "fresh")
            if age < self._options.ttl + self._options.stale_while_revalidate:
                return Hit(entry.hand_out(entry.context.model_copy(update={"origin": "stale"})), "stale")
            return Hit(entry.context, "expired")

    def _live(self, key: str) -> _Entry | None:
        """The entry for `key`, unless it is past `max_stale`, in which case it is dropped."""
        entry = self._entries.get(key)
        if entry is not None and time.monotonic() - entry.stored_at > self._options.max_stale:
            del self._entries[key]
            return None
        return entry

    def etag(self, key: str) -> str | None:
        with self._lock:
            entry = self._entries.get(key)
        return entry.context.etag if entry and entry.context.etag else None

    def absorb(self, key: str, scope: str, response: Context, *, deliver: bool = True) -> Context:
        """Folds an API answer into the cache and returns what the caller should see.

        `not_modified` means the pinned pack is unchanged: the cached text is kept, and what
        lives outside the pack (live turns, verification, withheld, timing) comes from the new
        answer. A `degraded` answer never replaces a good pack: the last good one is returned
        instead. A background refresh passes `deliver=False`, so its delta waits for a reader.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and response.not_modified:
                entry.context = entry.context.model_copy(
                    update={name: getattr(response, name) for name in _OUTSIDE_THE_PACK}
                )
                entry.stored_at = now
                self._pend(entry, response)
                self._entries.move_to_end(key)
                return self._deliver(entry, entry.context.model_copy(update={"origin": "cache"}), deliver)
            if entry is not None and response.degraded:
                self._pend(entry, response)
                self._entries.move_to_end(key)
                fallback = entry.context.model_copy(
                    update={"origin": "last_good", "elapsed_ms": response.elapsed_ms}
                )
                return self._deliver(entry, fallback, deliver)
            if response.not_modified or response.degraded:
                return response
            # A new pack already contains every change the deltas pending against the old one carried.
            same_pack = entry is not None and entry.context.etag == response.etag
            fresh = _Entry(response.model_copy(update={"delta": None}), now, scope)
            if same_pack and entry is not None:
                fresh.deltas = entry.deltas
            self._pend(fresh, response)
            self._entries[key] = fresh
            self._entries.move_to_end(key)
            while len(self._entries) > self._options.max_entries:
                self._entries.popitem(last=False)
            return self._deliver(fresh, response, deliver)

    @staticmethod
    def _pend(entry: _Entry, response: Context) -> None:
        if response.delta:
            entry.deltas.append(response.delta)

    @staticmethod
    def _deliver(entry: _Entry, context: Context, deliver: bool) -> Context:
        return entry.hand_out(context) if deliver else context

    def fallback(self, key: str, error: Exception) -> Context | None:
        """What to serve when the API failed: the last good pack, or None."""
        if self.drop_on_auth_error(key, error):
            return None
        with self._lock:
            entry = self._live(key)
            if entry is None:
                return None
            return entry.hand_out(entry.context.model_copy(update={"origin": "last_good"}))

    def drop_on_auth_error(self, key: str, error: Exception) -> bool:
        """401 and 403 are not outages: what was cached under that access goes with it.

        A revoked key loses everything; a source cut from one subject loses that pack.
        """
        if isinstance(error, AuthenticationError):
            self.clear()
            return True
        if isinstance(error, PermissionDeniedError):
            self.purge(key)
            return True
        return False

    def begin_refresh(self, key: str) -> bool:
        """Claims the background refresh of `key`; False if one is already running."""
        with self._lock:
            if key in self._refreshing:
                return False
            self._refreshing.add(key)
            return True

    def end_refresh(self, key: str) -> None:
        with self._lock:
            self._refreshing.discard(key)

    def purge(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def purge_scope(self, scope: str) -> None:
        """Drops every pack of one conversation or task, e.g. after its verification level changed."""
        with self._lock:
            for key in [k for k, entry in self._entries.items() if entry.scope == scope]:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
