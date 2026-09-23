"""Tunables of the client. The defaults follow the latency budgets the API is designed for."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timeouts:
    """Total time budgets in seconds, per method, independent of any platform timeout.

    Managed agent platforms give a turn 7 to 10 seconds and self-hosted frameworks give it
    none, so the SDK keeps its own: a missing context is better than a silent caller.
    `upload` bounds each attempt of sending media bytes to storage, off the hot path.
    """

    context: float = 0.30
    context_voice: float = 0.15
    navigation: float = 0.60
    navigation_voice: float = 0.30
    write: float = 5.0
    upload: float = 60.0


@dataclass(frozen=True)
class CacheOptions:
    """The per-conversation context cache, in seconds.

    A pack younger than `ttl` is served as is. For `stale_while_revalidate` more it is served
    at once while one background refresh per key revalidates it; after that the call waits for
    the API. When the API fails, the last good pack is served if it is younger than `max_stale`;
    older packs are dropped rather than shown to a model as if they were current. Past
    `max_entries` conversations, the least recently used pack goes.
    """

    enabled: bool = True
    ttl: float = 10.0
    stale_while_revalidate: float = 600.0
    max_stale: float = 1800.0
    max_entries: int = 1000


@dataclass(frozen=True)
class QueueOptions:
    """The local queue behind `track()`.

    A batch goes out when `batch_size` items are waiting or `interval` seconds after the
    first one arrived, whichever comes first. When `capacity` is reached new items are
    dropped and counted, so a long outage costs events, never memory.
    """

    capacity: int = 10_000
    batch_size: int = 15
    interval: float = 1.0
    heartbeat_interval: float = 60.0
