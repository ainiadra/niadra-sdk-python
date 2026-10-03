"""Tunables of the client. The defaults follow the latency budgets the API is designed for."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Timeouts:
    """Total time budgets in seconds, per method, independent of any platform timeout.

    Managed agent platforms give a turn 7 to 10 seconds and self-hosted frameworks give it
    none, so the SDK keeps its own: a missing context is better than a silent caller. Each
    budget covers the whole call, retries and waits included; only a call that must open the
    connection first gets `connect` on top, once. `write` is the budget of the
    writes a caller waits for (`identify`, `verify`, `feedback`, `subject_token` and the
    reservation of an upload) and the timeout of each attempt of the background queue, which
    never holds a caller. `upload` bounds sending media bytes to storage. `prefetch` bounds a
    prefetch, which runs in the background and never holds a turn, and a voice read that goes on
    after its turn's budget.

    `context` and `navigation` left at their defaults are what the API may take: the client measures
    the round trip to the region once when it starts (`VoiceOptions.probe`) and adds it on top, so an
    agent far from the region (Sao Paulo is 170 ms from us-east-2) is not timed out by the network.
    A value you set is a ceiling the SDK keeps; when the measured round trip plus 50 ms exceeds it,
    the client logs one warning, since every such read would run out of time. Before the measurement
    ends, the defaults apply as they are (with `connect` on top while no connection is open).

    In a voice conversation the pinned pack is read once and then served from memory, so
    `context_voice` is not a round trip: it is the most a turn waits for the read of its own words
    that a prefetch already started (see `niadra._voice`). A read starts when the partial transcript
    has been still for `VoiceOptions.settle` (0.2 s) and the platform ends the turn later (LiveKit
    waits at least 0.5 s of silence), which leaves about 0.3 s of head start; with 0.2 s of wait on
    top, the turn gets its slots while round trip plus server time stay under 0.5 s, a round trip
    of up to about 0.4 s at the server's p95. A longer wait would be heard: 0.2 s is the usual gap
    between two people's turns. `context_voice_start` bounds the first read of a call, made while
    the phone rings or the inbound webhook runs: a cold connection costs three round trips (TCP,
    TLS, the request) plus the server's first compile, 3 x 0.4 s + 0.3 s at a 0.4 s round trip.
    """

    context: float = 0.30
    context_voice: float = 0.20
    context_voice_start: float = 1.5
    navigation: float = 0.60
    navigation_voice: float = 0.30
    write: float = 5.0
    upload: float = 60.0
    prefetch: float = 1.0
    connect: float = 1.0
    """Added once to a budget when no connection to the API is likely open (no answer in the last two
    minutes): TCP and TLS take a few round trips, 0.3 s or more from another continent. 0 never adds it."""


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
class VoiceOptions:
    """The voice read path of a conversation in the `voice` view (see `niadra._voice`).

    `settle` is how long, in seconds, a partial transcript must stay the same before the SDK
    reads the turn with it. A read's slots answer the final turn when the final words start with
    the partial's and the partial carries at least `min_coverage` of them. `probe` measures the
    round trip to the region once, with `GET /healthz`, when the client starts (an `AsyncNiadra`
    built outside a running loop, at its first call): the default read budgets add it (`Timeouts`),
    and a warning says when a budget you set, or a voice budget once the client reads in voice,
    cannot hold it. `probe=False` measures nothing. `enabled=False` sends every voice turn to the
    API as the other views do.
    """

    enabled: bool = True
    settle: float = 0.2
    min_coverage: float = 0.75
    probe: bool = True


@dataclass(frozen=True)
class QueueOptions:
    """The local queue behind `track()`.

    A batch goes out when `batch_size` items are waiting, `interval` seconds after the first
    one arrived, or `turn_interval` seconds after the first conversation turn arrived (a
    message with a `conversation_id`), whichever comes first. A turn is what the other
    agents read in `live`, so by default it leaves at once, taking whatever else is waiting
    along. Only one batch is in flight per client: what is queued while one is answered
    leaves together as the next batch, so a burst of turns costs one request per round trip,
    never one per turn. When `capacity` is reached new items are dropped and counted, so a
    long outage costs events, never memory.
    """

    capacity: int = 10_000
    batch_size: int = 15
    interval: float = 1.0
    turn_interval: float = 0.0
    heartbeat_interval: float = 60.0


@dataclass(frozen=True)
class TurnOptions:
    """Turn recording (`client.turns`), apart from the event queue.

    `content_mode` fixes where recorded values go (`stored`, `pointer` or `hash_only`). By default it is
    `pointer` once a store is set (`turns.store()`), otherwise the mode the space's recording names, otherwise
    `stored`; a source that records less refuses more, and the SDK then sends digests only.

    The queue holds at most `max_bytes` of copied values and frames and `max_turns` turns: when full, it drops
    the values of unflagged turns first, and only then whole turns. A batch leaves `interval` seconds after
    its first turn closed, or at once with 50 turns waiting.
    """

    content_mode: Literal["stored", "pointer", "hash_only"] | None = None
    max_bytes: int = 64 * 1024 * 1024
    max_turns: int = 2000
    interval: float = 1.0
