"""The company's resolvers: functions of its own that read an object fresh from its source, inside its
boundary. Niadra never calls a company system; the SDK does, in the company's process.

```python
def requote(ref: StateRef, fields: Sequence[str] | None) -> dict:
    quote = pricing.quote(ref.id)
    return {"price_full": quote.full, "price_discounted": quote.discounted}

niadra.resolvers.register("health_quote", requote)
verdict = conversation.verify_claim("health_quote:op:q-77", "price_full", 511.06)
```

`verify_claim()` asks Niadra first (`POST /v1/state/verify`). A value that is not safe to claim (stale,
expired, never observed) is read again with the resolver of its type, within the claim's budget (300 ms by
default): the fresh value decides, and it enters the turn as an observation, so the claim contract and the
memory see it. A value is never verified from a stale copy: without a resolver, past the budget, or with the
resolver's circuit open (5 failures in 30 s open it for 30 s), the answer is `claim_safe: false` with the gap
said.

The same resolvers serve `niadra resolver-worker` (`niadra.cli.worker`), which takes the space's refresh
requests and pushes what they read.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from niadra._transport import Request
from niadra.models.state import StateRef, StateVerifyResponse, Verdict
from niadra.turns.capture import current_turn

logger = logging.getLogger("niadra")

CLAIM_BUDGET = 0.300
"""Seconds `verify_claim()` may take, the resolver's read included."""
BREAKER_FAILURES = 5
BREAKER_WINDOW = 30.0
BREAKER_OPEN = 30.0

Source = Literal["niadra", "resolver", "none"]


@dataclass(frozen=True)
class Resolved:
    """What a resolver read: the object's fields, observed now unless `observed_at` says when, and the
    source's version of the object when it has one (the push keeps a field only from a newer version)."""

    fields: Mapping[str, Any]
    version: int | None = None
    observed_at: datetime | None = None
    scope: Literal["global", "customer", "context"] = "global"


Resolver = Callable[[StateRef, "Sequence[str] | None"], "Mapping[str, Any] | Resolved | Awaitable[Any]"]


@dataclass(frozen=True)
class ClaimVerdict:
    """Whether a value may be claimed now: `claim_safe`, the field's `status`, whether the value `matches`
    what the source holds, who decided (`niadra`, `resolver`, or `none` when no one could), the gaps and
    prohibitions that stand, and the fresh `value` a resolver read."""

    claim_safe: bool
    status: Literal["fresh", "stale", "expired", "unknown"]
    matches: bool | None = None
    source: Source = "niadra"
    declared_gaps: tuple[str, ...] = ()
    prohibitions: tuple[str, ...] = ()
    value: Any = None


@dataclass
class _Breaker:
    failures: list[float] = field(default_factory=list)
    open_until: float = 0.0

    def closed(self, now: float) -> bool:
        return now >= self.open_until

    def failed(self, now: float) -> None:
        self.failures = [t for t in self.failures if now - t < BREAKER_WINDOW] + [now]
        if len(self.failures) >= BREAKER_FAILURES:
            self.open_until, self.failures = now + BREAKER_OPEN, []

    def succeeded(self) -> None:
        self.failures = []


@dataclass
class _Entry:
    fn: Resolver
    rate: float | None
    breaker: _Breaker = field(default_factory=_Breaker)
    next_at: float = 0.0


class Resolvers:
    """`niadra.resolvers`: the company's resolvers by object type, each with its circuit breaker and, for the
    worker, its rate."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._entries: dict[str, _Entry] = {}
        self._clock = clock
        self._lock = threading.Lock()
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None

    def register(self, type: str, fn: Resolver, *, rate: float | None = None) -> None:
        """`fn(ref, fields)` reads objects of `type` from their source: their fields as a mapping, or a
        `Resolved`. It may be a coroutine function. `rate` caps the worker's calls per second."""
        with self._lock:
            self._entries[type] = _Entry(fn, rate)

    def __contains__(self, type: object) -> bool:
        with self._lock:
            return type in self._entries

    @property
    def types(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    def available(self, type: str) -> bool:
        """A resolver for `type` whose circuit is closed."""
        with self._lock:
            entry = self._entries.get(type)
            return entry is not None and entry.breaker.closed(self._clock())

    def wait(self, type: str) -> float:
        """Seconds until the worker may call `type`'s resolver again, taking the slot."""
        with self._lock:
            entry = self._entries.get(type)
            if entry is None or entry.rate is None:
                return 0.0
            now = self._clock()
            at = max(now, entry.next_at)
            entry.next_at = at + 1.0 / entry.rate
            return at - now

    def resolve(self, ref: StateRef, fields: Sequence[str] | None, budget: float) -> Resolved | None:
        """Calls the resolver of `ref.type` from a synchronous caller, within `budget`; None when it failed,
        ran out of time or its circuit is open."""
        entry = self._entry(ref.type)
        if entry is None:
            return None
        pool = self._executor()
        future = pool.submit(_call_sync, entry.fn, ref, fields)
        try:
            return self._outcome(entry, future.result(timeout=budget))
        except Exception:
            future.cancel()
            return self._outcome(entry, None)

    async def aresolve(self, ref: StateRef, fields: Sequence[str] | None, budget: float) -> Resolved | None:
        """`resolve()` for an event loop: a coroutine resolver is awaited, any other runs in a thread."""
        entry = self._entry(ref.type)
        if entry is None:
            return None
        try:
            if inspect.iscoroutinefunction(entry.fn):
                found = await asyncio.wait_for(entry.fn(ref, fields), timeout=budget)
            else:
                found = await asyncio.wait_for(
                    asyncio.to_thread(_call_sync, entry.fn, ref, fields), timeout=budget
                )
        except Exception:
            return self._outcome(entry, None)
        return self._outcome(entry, found)

    def _entry(self, type: str) -> _Entry | None:
        with self._lock:
            entry = self._entries.get(type)
            return entry if entry is not None and entry.breaker.closed(self._clock()) else None

    def _outcome(self, entry: _Entry, found: Any) -> Resolved | None:
        resolved = _as_resolved(found)
        with self._lock:
            if resolved is None:
                entry.breaker.failed(self._clock())
            else:
                entry.breaker.succeeded()
        return resolved

    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="niadra-resolve")
            return self._pool


def _call_sync(fn: Resolver, ref: StateRef, fields: Sequence[str] | None) -> Any:
    found = fn(ref, fields)
    if inspect.isawaitable(found):
        return asyncio.run(_awaited(found))
    return found


async def _awaited(found: Awaitable[Any]) -> Any:
    return await found


def _as_resolved(found: Any) -> Resolved | None:
    if isinstance(found, Resolved):
        return found
    if isinstance(found, Mapping):
        return Resolved(dict(found))
    return None


def as_ref(ref: StateRef | Mapping[str, Any] | str) -> StateRef:
    """A reference as `type:namespace:id`, a mapping or a `StateRef`."""
    if isinstance(ref, StateRef):
        return ref
    if isinstance(ref, str):
        kind, _, rest = ref.partition(":")
        namespace, _, object_id = rest.partition(":")
        return StateRef(type=kind, namespace=namespace, id=object_id)
    return StateRef.model_validate(ref)


def verify_http(ref: StateRef, field: str, value: Any, subject: Any, budget: float) -> Request:
    check = {"ref": ref.model_dump(mode="json", exclude_none=True), "field": field, "value": value}
    body: dict[str, Any] = {"checks": [check]}
    if subject is not None:
        body["subject"] = subject.model_dump(mode="json", exclude_none=True)
    return Request("POST", "/v1/state/verify", json=body, timeout=budget, budget=budget, max_attempts=1)


def verdict_of(data: Any) -> Verdict | None:
    verdicts = StateVerifyResponse.model_validate(data).verdicts
    return verdicts[0] if verdicts else None


def from_niadra(verdict: Verdict) -> ClaimVerdict:
    return ClaimVerdict(
        verdict.claim_safe,
        verdict.status,
        verdict.matches,
        "niadra",
        tuple(verdict.declared_gaps),
        tuple(verdict.prohibitions),
    )


def from_resolver(
    ref: StateRef, field: str, value: Any, resolved: Resolved | None, verdict: Verdict | None
) -> ClaimVerdict:
    """The verdict once the resolver answered (or not): a fresh value decides; nothing else verifies."""
    gaps = tuple(verdict.declared_gaps) if verdict is not None else ()
    prohibitions = tuple(verdict.prohibitions) if verdict is not None else ()
    if resolved is None or field not in resolved.fields:
        status = verdict.status if verdict is not None else "unknown"
        return ClaimVerdict(False, status, None, "none", (*gaps, "source_unreachable"), prohibitions)
    fresh = resolved.fields[field]
    matches = same(fresh, value)
    _observed(ref, resolved)
    return ClaimVerdict(matches, "fresh", matches, "resolver", gaps, (), fresh)


def same(a: Any, b: Any) -> bool:
    """Equal values, numbers by their decimal value (511.06 and "511.06" are the same)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError):
        return bool(a == b)


def _observed(ref: StateRef, resolved: Resolved) -> None:
    """The fresh read enters the turn as a call that observed the object, as a tool's result would."""
    frame = current_turn()
    if frame is None:
        return
    at = resolved.observed_at or datetime.now(timezone.utc)
    observation = {
        "ref": f"{ref.type}:{ref.namespace}:{ref.id}",
        "fields": dict(resolved.fields),
        "provenance": {"source": "live", "source_observed_at": at.isoformat(), "scope": resolved.scope},
    }
    with frame.tool_call(f"resolve:{ref.type}", {"ref": observation["ref"]}, synthetic=False) as call:
        call.result(dict(resolved.fields), observations=[observation])


def push_item(ref: StateRef, resolved: Resolved) -> dict[str, Any]:
    """What the worker pushes for one resolved object (`POST /v1/objects/push`)."""
    at = resolved.observed_at or datetime.now(timezone.utc)
    version = resolved.version if resolved.version is not None else int(at.timestamp() * 1000)
    return {
        "ref": ref.model_dump(mode="json", exclude_none=True),
        "fields": dict(resolved.fields),
        "provenance": {"source": "live", "source_observed_at": at.isoformat(), "scope": resolved.scope},
        "version": version,
    }
