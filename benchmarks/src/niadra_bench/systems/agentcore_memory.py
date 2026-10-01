"""Amazon Bedrock AgentCore Memory (AWS's managed memory for agents), driven the way its developer guide
shows ("Get started with AgentCore Memory", "Enable long-term memory", "Ingest content into long-term
memory", "Retrieve memory records"), in the region the benchmark runs in. A managed service: there is no
container to run, so the harness reaches it through a small signing service of its own on the host
(`bench serve agentcore-proxy`, `deploy/systems/agentcore-memory`), which forwards the documented paths.

- The resource: one AgentCore Memory per identity scenario, created by the run (CreateMemory) and deleted
  when it ends, with the three built-in long-term strategies the guide and AWS's customer support examples
  use together (semantic, user preference, summary), each under the customer's namespace root
  (`/customers/{actorId}/...`), and raw events kept 365 days. AWS runs and bills the strategies' model
  inference: no model, prompt or embedder of AgentCore's can be set to the benchmark's, and none is.
  Episodic is not on (config/agentcore.toml says why).
- Writes: each exchange of a conversation (the customer's message and the agent's answer) is one
  CreateEvent with both messages as `conversational` payload (`USER`, `ASSISTANT`), the customer's
  `actorId`, one `sessionId` per conversation, the time it happened as `eventTimestamp` and a new
  `clientToken` (the idempotency token the AWS SDKs add by themselves). A CRM or ERP
  record is no conversation: it goes through IngestData as a `json` payload, the route the guide gives
  for system events, with its time.
- Identity: AgentCore keys memory by `actorId` and resolves no identity. It runs in both scenarios, as
  Mem0 does: `known_id` (one actor id on every channel, its best case) and `per_channel_id` (each
  channel's own identifier as the actor id, in the characters an actor id may have).
- Settle: the extraction is asynchronous. The run waits until every session written has a summary record
  (ListMemoryRecords on the summary strategy), then until the number of records stays the same.
- Reads: one RetrieveMemoryRecords per turn, the question as `searchQuery`, the customer's root as
  `namespacePath` (records of every strategy under it) and `topK` 10, the API's default. The agent
  receives each record's text as the service returns it (a fact, a preference as JSON, a summary as XML).
- Path: the timed lines are named `region`, not `host`: each call leaves the harness's host for the
  service's endpoint in the same region (TLS, a kept connection), where every other system answers on the
  host itself.
- A live exchange (metrics 6 and 9) is one CreateEvent. Metric 6 reads once a second, not every 50 ms:
  AWS bills each retrieval and the guide gives the extraction "seconds to minutes".
- Open (metric 8): GetMemoryRecord on the first record.
- Cost: AWS's public prices for what AgentCore charges (config/agentcore.toml): one CreateEvent and one
  retrieval per exchange, the strategies' models included; the records' storage, per month kept, apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from niadra_bench import config as bench_config
from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import SCENARIOS, Identities
from niadra_bench.metrics import latency
from niadra_bench.systems.base import Call, HttpSystem

TURN_SPACING_S = 40
SETTINGS_FILE = "agentcore.toml"
_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")
SESSION_ID_MAX = 100
ACTOR_ID_MAX = 255
PAGE = 100


def settings() -> dict[str, Any]:
    with (bench_config.CONFIG_DIR / SETTINGS_FILE).open("rb") as handle:
        return tomllib.load(handle)


def actor_id(raw: str) -> str:
    """An identifier in the characters AgentCore's `actorId` takes (it must start with a letter or digit;
    `+`, `@` and `.` of a phone number or an e-mail are not allowed)."""
    safe = _UNSAFE.sub("-", raw).lstrip("-_") or "x"
    return safe[:ACTOR_ID_MAX]


def session_id(actor: str, session: str) -> str:
    """One `sessionId` per conversation of a customer, within the 100 characters the API takes."""
    value = _UNSAFE.sub("-", f"{actor}-{session}").lstrip("-_")
    if len(value) <= SESSION_ID_MAX:
        return value
    return f"{value[: SESSION_ID_MAX - 13]}-{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def exchange_events(session: Session, start: datetime) -> list[tuple[float, list[dict[str, Any]]]]:
    """The session's turns as CreateEvent payloads: each customer message with the agent's answer after
    it, and the time of the exchange's first message (epoch seconds)."""
    events: list[tuple[float, list[dict[str, Any]]]] = []
    current: list[dict[str, Any]] = []
    at = start
    for index, turn in enumerate(session.turns):
        role = "USER" if turn.role == "customer" else "ASSISTANT"
        if role == "USER" and current:
            events.append((at.timestamp(), current))
            current = []
        if not current:
            at = start + timedelta(seconds=TURN_SPACING_S * index)
        current.append({"conversational": {"role": role, "content": {"text": turn.text}}})
    if current:
        events.append((at.timestamp(), current))
    return events


class AgentCoreMemory(HttpSystem):
    system = "agentcore_memory"
    title = "Amazon Bedrock AgentCore Memory"
    compose = "agentcore-memory"
    url_env = "AGENTCORE_URL"
    default_url = "http://agentcore:8090"
    # A managed service has no release to pin: the data plane's API version, and the run's date says when.
    version = "bedrock-agentcore 2024-02-28 (managed, built-in strategies)"
    has_open = True
    min_settle_timeout_s = 2 * 3600.0
    # Not on the harness's host: the service's endpoint in the region, through the signing proxy.
    path = latency.REGION
    #: The identity scenarios the runner measures this system in (one instance each).
    scenarios: ClassVar[tuple[str, ...]] = SCENARIOS
    #: The settle step: seconds between two looks, how long the record count must stay the same, and how
    #: long the summaries may stay short of the sessions written before the count alone decides.
    settle_poll_s: ClassVar[float] = 30.0
    settle_quiet_s: ClassVar[float] = 60.0
    settle_stall_s: ClassVar[float] = 600.0
    #: Metric 6 reads at most this often: every retrieval is billed.
    fresh_poll_s: ClassVar[float] = 1.0
    create_timeout_s: ClassVar[float] = 660.0

    def __init__(self, *, scenario: str = "known_id", now: datetime | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown identity scenario {scenario!r}")
        self.scenario = scenario
        self._now = now
        self.settings = settings()
        self.memory_id = "unset"
        self.strategies: dict[str, str] = {}
        self._sessions: set[str] = set()
        # The writes this instance built: CreateEvent and IngestData requests (seeding and live exchanges).
        self._writes = 0
        self._measured: dict[str, Any] = {}

    # The resource

    async def start(self) -> None:
        await super().start()
        memory = self.settings["memory"]
        suffix = "known" if self.scenario == "known_id" else "channel"
        spec = {
            "name": f"niadra_bench_{uuid.uuid4().hex[:8]}_{suffix}",
            "event_expiry_days": memory["event_expiry_days"],
            "strategies": memory["strategies"],
            "namespace_root": memory["namespace_root"],
        }
        response = await self.http.post(f"{self.url}/_bench/memory", json=spec, timeout=self.create_timeout_s)
        if response.status_code != 200:
            raise RuntimeError(
                f"AgentCore Memory was not created: {response.status_code} {response.text[:600]}"
            )
        created = response.json()
        self.memory_id = created["memory_id"]
        self.strategies = created["strategies"]

    async def close(self) -> None:
        if self._http is not None and self.memory_id != "unset":
            with contextlib.suppress(httpx.HTTPError):
                await self.http.delete(f"{self.url}/_bench/memory/{self.memory_id}", timeout=60)
        await super().close()

    def health_call(self) -> Call:
        return Call("GET", "/healthz")

    # Identity

    def actor(self, ids: Identities, channel: str) -> str:
        return actor_id(ids.mem0_user(self.scenario, channel))  # type: ignore[arg-type]

    def customer(self, ids: Identities) -> str:
        return self.actor(ids, "whatsapp")

    def root(self, actor: str) -> str:
        return str(self.settings["memory"]["namespace_root"]).replace("{actorId}", actor)

    def _path(self, rest: str) -> str:
        return f"/memories/{self.memory_id}{rest}"

    # Calls

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        now = self._now or datetime.now(UTC)
        calls: list[Call] = []
        for session in case.chronological():
            actor = self.actor(ids, session.channel)
            sid = session_id(actor, session.id)
            self._sessions.add(sid)
            start = now - timedelta(days=session.days_ago)
            if session.record is not None:
                record = {
                    "source": session.channel,
                    "type": session.record.kind,
                    "record": ids.fill(session.record.text, case.customer.name),
                }
                body = {
                    "clientToken": uuid.uuid4().hex,
                    "actorId": actor,
                    "sessionId": sid,
                    "contentTimestamp": start.timestamp(),
                    "source": {"inline": {"payload": [{"json": {"content": record}}]}},
                }
                calls.append(Call("POST", self._path("/ingest"), json=body))
                self._writes += 1
            for at, payload in exchange_events(session, start):
                event = {
                    "clientToken": uuid.uuid4().hex,
                    "actorId": actor,
                    "sessionId": sid,
                    "eventTimestamp": at,
                    "payload": payload,
                }
                calls.append(Call("POST", self._path("/events"), json=event))
                self._writes += 1
        return calls

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:
        body = {
            "namespacePath": self.root(self.actor(ids, case.probe.channel)),
            "searchCriteria": {"searchQuery": question, "topK": int(self.settings["read"]["top_k"])},
        }
        return Call("POST", self._path("/retrieve"), json=body)

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        records = response.json().get("memoryRecordSummaries") or []
        return [text for r in records if (text := str((r.get("content") or {}).get("text") or "").strip())]

    def open_call(self, case: Case, ids: Identities, read: httpx.Response) -> Call | None:  # noqa: ARG002
        records = read.json().get("memoryRecordSummaries") or []
        first = next((str(r["memoryRecordId"]) for r in records if r.get("memoryRecordId")), None)
        return Call("GET", self._path(f"/memoryRecord/{first}")) if first else None

    def exchange_call(
        self,
        case: Case,  # noqa: ARG002
        ids: Identities,
        conversation: str,
        customer: str,
        agent: str | None,
    ) -> Call:
        actor = self.actor(ids, "whatsapp")
        sid = session_id(actor, conversation)
        self._sessions.add(sid)
        self._writes += 1
        payload = [{"conversational": {"role": "USER", "content": {"text": customer}}}]
        if agent:
            payload.append({"conversational": {"role": "ASSISTANT", "content": {"text": agent}}})
        body = {
            "clientToken": uuid.uuid4().hex,
            "actorId": actor,
            "sessionId": sid,
            "eventTimestamp": datetime.now(UTC).timestamp(),
            "payload": payload,
        }
        return Call("POST", self._path("/events"), json=body)

    def retry_of(self, call: Call, response: httpx.Response) -> Call | None:
        # RetryableConflictException: the API asks for the same request again.
        if response.status_code == 409:
            return call
        response.raise_for_status()
        return None

    async def freshness_trial(
        self, case: Case, ids: Identities, interval_s: float, timeout_s: float
    ) -> float | None:
        return await super().freshness_trial(case, ids, max(interval_s, self.fresh_poll_s), timeout_s)

    # Settle

    async def _list(self, strategy: str | None) -> tuple[list[dict[str, Any]], int]:
        """Every record under the customers' root (of one strategy, or all), and the calls it took."""
        prefix = str(self.settings["memory"]["namespace_root"]).split("{", 1)[0]
        records: list[dict[str, Any]] = []
        calls = 0
        token: str | None = None
        while True:
            body: dict[str, Any] = {"namespacePath": prefix, "maxResults": PAGE}
            if strategy:
                body["memoryStrategyId"] = strategy
            if token:
                body["nextToken"] = token
            call = Call("POST", self._path("/memoryRecords"), json=body)
            for attempt in range(8):
                response = await call.send(self.http, self.url, self.headers)
                if response.status_code not in (429, 500, 502, 503):
                    break
                await asyncio.sleep(min(10.0, 1.0 + attempt))
            calls += 1
            response.raise_for_status()
            data = response.json()
            records += data.get("memoryRecordSummaries") or []
            token = data.get("nextToken")
            if not token:
                return records, calls

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:  # noqa: ARG002
        """Every session written has a summary record, then the number of records stays the same."""
        started = time.monotonic()
        summary = self.strategies.get("summaries")
        expected = len(self._sessions)
        list_calls = rounds = summarized = 0
        last_change = time.monotonic()
        while summary and time.monotonic() - started < self.settle_timeout_s:
            rounds += 1
            found, calls = await self._list(summary)
            list_calls += calls
            seen = len({(r.get("namespaces") or [""])[0] for r in found})
            if seen != summarized:
                summarized, last_change = seen, time.monotonic()
            if summarized >= expected or time.monotonic() - last_change >= self.settle_stall_s:
                break
            await asyncio.sleep(self.settle_poll_s)
        total = -1
        records: list[dict[str, Any]] = []
        quiet_since = time.monotonic()
        stable = False
        while time.monotonic() - started < self.settle_timeout_s:
            records, calls = await self._list(None)
            list_calls += calls
            if len(records) != total:
                total, quiet_since = len(records), time.monotonic()
            elif time.monotonic() - quiet_since >= self.settle_quiet_s:
                stable = True
                break
            await asyncio.sleep(self.settle_poll_s)
        by_name = {identifier: name for name, identifier in self.strategies.items()}
        by_strategy: dict[str, int] = {}
        for record in records:
            name = by_name.get(str(record.get("memoryStrategyId")), "other")
            by_strategy[name] = by_strategy.get(name, 0) + 1
        meter = await self._meter()
        self._measured = {"by_strategy": by_strategy, "sessions": expected, "writes": self._writes}
        return {
            "settled": stable,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "sessions_written": expected,
            "writes": self._writes,
            "sessions_summarized": summarized,
            "records": max(total, 0),
            "records_by_strategy": by_strategy,
            "list_calls": list_calls,
            "memory_id": self.memory_id,
            "configuration": {**self.settings["memory"], **self.settings["read"]},
            "requests": meter,
        }

    async def _meter(self) -> dict[str, int]:
        """The proxy's counters: the requests AWS accepted, by kind (both scenarios' memories together)."""
        try:
            response = await self.http.get(f"{self.url}/_meter", timeout=10)
            response.raise_for_status()
            counters: dict[str, int] = response.json().get("requests") or {}
        except (httpx.HTTPError, ValueError):
            return {}
        return counters

    # Cost

    def cost_row(self) -> dict[str, Any] | None:
        """Metric 3 from AWS's public prices: per exchange, one CreateEvent and one RetrieveMemoryRecords
        (the built-in strategies' model inference is in those prices). The records the strategies made are
        billed per month kept, so they are given apart, from what the run measured: the facts and
        preferences per write, times the exchanges of a conversation, and the summaries per session."""
        prices = self.settings["prices"]
        turns = bench_config.load().cost.turns_per_conversation
        per_exchange = float(prices["event"]) + float(prices["retrieval"])
        row: dict[str, Any] = {
            "variant": "service_price",
            "memory_usd_per_1000": round(per_exchange * turns * 1000, 4),
            "basis": (
                "AWS public prices: one CreateEvent and one RetrieveMemoryRecords per exchange, the "
                "built-in strategies' model inference included; record storage apart, per month kept"
            ),
            "usd_per_event": float(prices["event"]),
            "usd_per_retrieval": float(prices["retrieval"]),
            "usd_per_record_month": float(prices["record_month"]),
            "prices_checked_on": str(prices["checked_on"]),
        }
        counts: dict[str, int] = self._measured.get("by_strategy") or {}
        writes, sessions = int(self._measured.get("writes") or 0), int(self._measured.get("sessions") or 0)
        if counts and writes and sessions:
            per_write = (sum(counts.values()) - counts.get("summaries", 0)) / writes
            per_session = counts.get("summaries", 0) / sessions
            row["records_per_write"] = round(per_write, 3)
            row["summary_records_per_session"] = round(per_session, 3)
            row["storage_usd_per_1000_per_month"] = round(
                (per_write * turns + per_session) * 1000 * float(prices["record_month"]), 4
            )
        return row
