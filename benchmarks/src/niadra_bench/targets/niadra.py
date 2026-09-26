"""Niadra through its published Python SDK (`niadra` on PyPI) and its HTTP API.

Seeding posts each session to `POST /v1/batch` with the SDK's own item models, the same bytes
`track()` sends, but synchronously, so a rejected event fails the run instead of being dropped by a
background queue. Reading uses `AsyncNiadra.context()`, what an agent calls before its model.

Keys: `NIADRA_BOOTSTRAP` (the sandbox tenant's bootstrap.json, one key per source) or `NIADRA_API_KEY`
(one key for every channel). `NIADRA_BASE_URL` overrides the address the SDK derives from the key. With
`NIADRA_CONTROL_URL` and the bootstrap's admin account, the billing agent gets a source of its own that
declares the dataset's operations, as the docs tell a customer to set it up (see `sources.py`). With
`memory_v2` set (`bench run --niadra-memory-v2 on|off`), the same account sets the space's `memory_v2`
flag before seeding and puts the old value back when the run ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import niadra
from niadra import (
    AsyncNiadra,
    CacheOptions,
    Content,
    EventItem,
    ObjectRef,
    Speaker,
    SpeakerRef,
    Verification,
)
from niadra.models.events import ActionInfo, ConversationEndedItem

from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import Identities
from niadra_bench.sources import MEMORY_V2_FLAG, ControlPlane, IssuedKey, wait_until_served
from niadra_bench.targets.base import Retrieved, Stopwatch, Target

# The source key each channel writes and reads with, when the bootstrap has one per source.
CHANNEL_SOURCE = {
    "whatsapp": ("whatsapp",),
    "voice": ("voice", "whatsapp"),
    "email": ("email", "whatsapp"),
    "app": ("app", "whatsapp"),
    "crm": ("crm", "billing", "whatsapp"),
    "erp": ("erp", "billing", "whatsapp"),
}
OBJECT_NAMESPACE = {"ticket": "crm", "claim": "core", "dispute": "core", "order": "erp", "shipment": "tms"}
TURN_SPACING_S = 40
RETRIEVE_TIMEOUT_S = 5.0

log = logging.getLogger(__name__)


class Pacer:
    """At most `per_s` starts a second across every task that waits on it, and a pause for all of them
    when the server says it is overloaded. None: no limit (dry runs)."""

    def __init__(self, per_s: float | None) -> None:
        self.interval = 1 / per_s if per_s else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self.interval
        if at > now:
            await asyncio.sleep(at - now)

    def pause(self, seconds: float) -> None:
        self._next = max(self._next, time.monotonic() + seconds)


class Keys:
    def __init__(self, by_source: dict[str, str], document: dict[str, Any] | None = None) -> None:
        if not by_source:
            raise ValueError("no Niadra key: set NIADRA_BOOTSTRAP or NIADRA_API_KEY")
        self.by_source = by_source
        self.document = document

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Keys:
        source = os.environ if env is None else env
        if path := source.get("NIADRA_BOOTSTRAP"):
            document = json.loads(Path(path).read_text())
            return cls({str(k): str(v) for k, v in document["keys"].items()}, document)
        if key := source.get("NIADRA_API_KEY"):
            return cls({"whatsapp": key})
        return cls({})

    def for_channel(self, channel: str) -> tuple[str, str]:
        for name in CHANNEL_SOURCE.get(channel, ("whatsapp",)):
            if name in self.by_source:
                return name, self.by_source[name]
        name = next(iter(self.by_source))
        return name, self.by_source[name]


def session_items(case: Case, ids: Identities, session: Session, now: datetime) -> list[dict[str, Any]]:
    """One session as batch items, with idempotency keys stable for the run tag."""
    start = now - timedelta(days=session.days_ago)
    prefix = f"bench-{ids.tag}-{case.id}-{session.id}"
    hint = Verification.V2 if session.sensitive else None
    items: list[dict[str, Any]] = []
    if session.record is not None:
        record = session.record
        handles = ids.all_handles() if record.links_all_handles else [ids.handle("crm_id")]
        object_id = ids.fill(record.object_id, case.customer.name)
        if record.object_type != "customer":
            object_id = f"{object_id}-{ids.tag}"
        namespace = (
            "crm" if record.object_type == "customer" else OBJECT_NAMESPACE.get(record.object_type, "erp")
        )
        ref = ObjectRef(type=record.object_type, namespace=namespace, id=object_id)
        if record.kind == "action":
            event = EventItem(
                kind="action",
                idempotency_key=f"{prefix}-record",
                channel=session.channel,
                handles=handles,
                object_refs=[ref],
                speaker=SpeakerRef(role=Speaker.AI_AGENT, id="billing-agent"),
                action=ActionInfo(operation=record.operation or "update", result=record.text),
                occurred_at=start,
            )
        else:
            event = EventItem(
                kind="system_event",
                idempotency_key=f"{prefix}-record",
                channel=session.channel,
                handles=handles,
                object_refs=[] if record.object_type == "customer" else [ref],
                speaker=SpeakerRef(role=Speaker.SYSTEM),
                canonical_type=record.canonical_type,
                fields=dict(record.fields),
                occurred_at=start,
            )
        items.append(event.model_dump(mode="json", exclude_none=True))
    if session.turns:
        conversation_id = f"{prefix}-conv"
        handle = ids.channel_handle(session.channel)
        for index, turn in enumerate(session.turns):
            customer = turn.role == "customer"
            event = EventItem(
                kind="message",
                idempotency_key=f"{prefix}-t{index}",
                channel=session.channel,
                conversation_id=conversation_id,
                handles=[handle],
                speaker=SpeakerRef(role=Speaker.CUSTOMER if customer else Speaker.AI_AGENT),
                direction="inbound" if customer else "outbound",
                content=Content(text=turn.text),
                occurred_at=start + timedelta(seconds=TURN_SPACING_S * index),
                verification_hint=hint,
            )
            items.append(event.model_dump(mode="json", exclude_none=True))
        ended = ConversationEndedItem(
            idempotency_key=f"{prefix}-end",
            conversation_id=conversation_id,
            occurred_at=start + timedelta(seconds=TURN_SPACING_S * len(session.turns)),
        )
        items.append(ended.model_dump(mode="json", exclude_none=True))
    return items


class NiadraTarget(Target):
    system = "niadra"

    def __init__(
        self,
        keys: Keys,
        *,
        base_url: str | None = None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        now: Callable[[], datetime] | None = None,
        settle_quiet_s: float = 20.0,
        settle_timeout_s: float = 1200.0,
        concurrency: int = 8,
        control_url: str | None = None,
        operations: list[str] | None = None,
        memory_v2: bool | None = None,
        seed_rate: float | None = None,
        seed_concurrency: int | None = None,
        settle_interval_s: float = 0.0,
        pause_on_overload_s: float = 0.0,
    ) -> None:
        self.keys = keys
        self.base_url = base_url or os.environ.get("NIADRA_BASE_URL") or None
        self._transport_factory = transport_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._clients: dict[str, AsyncNiadra] = {}
        self._http: httpx.AsyncClient | None = None
        self._probe_counter = 0
        self.settle_quiet_s = settle_quiet_s
        self.settle_timeout_s = settle_timeout_s
        self._limit = asyncio.Semaphore(concurrency)
        # The caps on what a run sends to production (config [production]); None in dry runs.
        self._pacer = Pacer(seed_rate)
        self.seed_concurrency = seed_concurrency
        self.settle_interval_s = settle_interval_s
        self.pause_on_overload_s = pause_on_overload_s
        self.overloads = 0
        self.stale_keys_revoked: list[str] = []
        self.refused: list[str] = []
        self.control_url = control_url
        self.operations = operations or []
        self._billing: IssuedKey | None = None
        self._control: ControlPlane | None = None
        self.memory_v2 = memory_v2
        self.memory_v2_flag = os.environ.get("NIADRA_MEMORY_V2_FLAG") or MEMORY_V2_FLAG
        # What the space had before the run set the flag, to put back at the end.
        self._flag_before: tuple[bool | None] | None = None

    def _new_http(self, timeout: float = 30.0) -> httpx.AsyncClient:
        transport = self._transport_factory() if self._transport_factory else None
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    def client(self, channel: str) -> AsyncNiadra:
        source, key = self.keys.for_channel(channel)
        if source not in self._clients:
            self._clients[source] = AsyncNiadra(
                key,
                base_url=self.base_url,
                channel=channel,
                cache=CacheOptions(enabled=False),
                http_client=self._new_http() if self._transport_factory else None,
            )
        return self._clients[source]

    async def start(self) -> None:
        self._http = self._new_http()
        if self.control_url and self.keys.document:
            self._control = ControlPlane(self.control_url, self.keys.document, self._http)
        if self.memory_v2 is not None:
            if self._control is None:
                raise RuntimeError(
                    "--niadra-memory-v2 needs NIADRA_CONTROL_URL and the bootstrap's admin account"
                )
            # Before the billing key: the key's wait below ends when the cell serves a configuration
            # snapshot that also carries the flag.
            before = await self._control.set_flag(self.memory_v2_flag, self.memory_v2)
            self._flag_before = (before,)
            log.info("memory_v2 %s for this run (was %s)", self.memory_v2, before)
        if self._control is not None and self.operations:
            self.stale_keys_revoked = await self._control.revoke_stale_keys()
            if self.stale_keys_revoked:
                log.warning(
                    "revoked %s billing keys left active by earlier runs", len(self.stale_keys_revoked)
                )
            self._billing = await self._control.billing_key(self.operations)
            self.keys.by_source["billing"] = self._billing.secret
            self._clients.pop("billing", None)
            await wait_until_served(self._http, self.client("erp").base_url, self._billing.secret)
        elif self.memory_v2 is not None:
            log.warning("memory_v2 set with no billing key to wait on: the first reads may predate it")

    async def _post_batch(self, channel: str, items: list[dict[str, Any]]) -> None:
        assert self._http is not None
        client = self.client(channel)
        _, key = self.keys.for_channel(channel)
        headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
        for _attempt in range(12):
            await self._pacer.wait()
            response = await self._http.post(
                f"{client.base_url}/v1/batch", json={"items": items}, headers=headers
            )
            if response.status_code in (429, 503):
                # Every seeding task waits, not only this one: the server asked for less.
                self.overloads += 1
                wait = max(float(response.headers.get("retry-after") or 2), self.pause_on_overload_s)
                self._pacer.pause(wait)
                continue
            if response.status_code not in (200, 207):
                raise RuntimeError(f"batch rejected with {response.status_code}: {response.text[:300]}")
            body = response.json()
            errors = body.get("errors") or []
            refused = [e for e in errors if e.get("code") == "operation_not_allowed"]
            if len(refused) < len(errors):
                raise RuntimeError(f"batch items rejected: {errors[:3]}")
            # An action whose operation the source did not declare is refused by design (the sandbox's
            # billing source trusts only `credit`). The rest of the history is still written, and the
            # refusal is counted in the results instead of failing the case.
            for error in refused:
                item = items[int(error.get("index", 0))]
                self.refused.append(
                    f"{item.get('idempotency_key')}: {item.get('action', {}).get('operation')}"
                )
            return
        raise RuntimeError("the batch kept answering 429/503")

    def seed_report(self) -> dict[str, Any]:
        refused, self.refused = self.refused, []
        declared = self.operations if self._billing else None
        overloads, self.overloads = self.overloads, 0
        stale, self.stale_keys_revoked = self.stale_keys_revoked, []
        return {
            "refused_actions": sorted(refused),
            "declared_operations": declared,
            **({"memory_v2": self.memory_v2} if self.memory_v2 is not None else {}),
            **({"overload_pauses": overloads} if overloads else {}),
            **({"stale_keys_revoked": len(stale)} if stale else {}),
        }

    async def seed(self, case: Case, ids: Identities) -> None:
        now = self._now()
        for session in case.chronological():
            await self._post_batch(session.channel, session_items(case, ids, session, now))

    async def _version(self, case: Case, ids: Identities) -> tuple[str, str]:
        async with self._limit:
            context = await self.client(case.probe.channel).context(
                ids.channel_handle(case.probe.channel),
                verification="V1",
                timeout=RETRIEVE_TIMEOUT_S,
                use_cache=False,
            )
        return context.version, context.etag

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:
        """Polls every case's context until none changed for `settle_quiet_s` (extraction is async)."""
        started = time.monotonic()
        last: dict[str, tuple[str, str]] = {}
        quiet_since = time.monotonic()
        rounds = 0
        while True:
            rounds += 1
            versions = await asyncio.gather(*(self._version(c, i) for c, i in pairs))
            current = {i.case_id: v for (_, i), v in zip(pairs, versions, strict=True)}
            if current != last:
                last = current
                quiet_since = time.monotonic()
            elapsed = time.monotonic() - started
            if time.monotonic() - quiet_since >= self.settle_quiet_s:
                return {"settled": True, "seconds": round(elapsed, 1), "rounds": rounds}
            if elapsed >= self.settle_timeout_s:
                return {"settled": False, "seconds": round(elapsed, 1), "rounds": rounds}
            await asyncio.sleep(max(self.settle_interval_s, min(5.0, max(0.5, self.settle_quiet_s / 4))))

    async def retrieve(self, case: Case, ids: Identities, *, view: str | None = None) -> Retrieved:
        self._probe_counter += 1
        conversation = f"bench-{ids.tag}-{case.id}-probe-{self._probe_counter}"
        client = self.client(case.probe.channel)
        handle = ids.channel_handle(case.probe.channel)
        level = case.probe.verification
        view = view or ("voice" if case.probe.channel == "voice" else "chat")
        async with self._limit:
            if level != "V0" and case.probe.verify_method:
                await client.verify(
                    case.probe.verify_method,  # type: ignore[arg-type]
                    level,
                    handle=handle,
                    conversation_id=conversation,
                )
            watch = Stopwatch()
            context = await client.context(
                handle,
                view=view,
                verification=level,
                conversation_id=conversation,
                query=case.probe.question,
                timeout=RETRIEVE_TIMEOUT_S,
                use_cache=False,
            )
            elapsed = watch.ms
        text = "\n\n".join(part for part in (context.system_block, context.turn_block) if part)
        return Retrieved(
            text=text,
            elapsed_ms=elapsed,
            error=context.error,
            meta={
                "path": str(context.path),
                "withheld": context.withheld,
                "verification": context.verification.effective.value,
                "view": view,
            },
        )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close(timeout=2.0)
        self._clients.clear()
        if self._control is not None and self._billing is not None:
            try:
                await self._control.revoke(self._billing)
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("the run's billing key was not revoked: %s", exc)
            self._billing = None
        if self._control is not None and self._flag_before is not None:
            (before,) = self._flag_before
            try:
                await self._control.set_flag(self.memory_v2_flag, before)
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("memory_v2 was not put back to %s: %s", before, exc)
            self._flag_before = None
        if self._http is not None:
            await self._http.aclose()

    def versions(self) -> dict[str, str]:
        return {"niadra_sdk": niadra.__version__}
