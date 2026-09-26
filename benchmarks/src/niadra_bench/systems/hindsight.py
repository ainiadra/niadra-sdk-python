"""Hindsight (github.com/vectorize-io/hindsight, MIT), its one-container image at 0.10.1 with the
embedded PostgreSQL, driven as its documentation shows (hindsight-docs, "Retaining a Conversation"):

- Writes: one memory bank per customer (Hindsight has no identity across channels, so this is Mem0's
  `known_id`, its best case). Each session is one `retain` item, as the docs ask for a conversation: the
  whole conversation as `Name (timestamp): text` lines, the time it happened as `timestamp`, a short
  `context` label with the channel, and a `document_id` per conversation. A CRM or ERP record is its own
  item with its time. Retain runs synchronously (`async` false, its default), so the call returns after
  the facts were extracted.
- Settle: the bank's background work (consolidation into observations) is waited for through
  `GET .../operations` until nothing is pending or processing.
- Reads: `POST .../memories/recall` with the customer's question and its defaults (budget `mid`,
  `max_tokens` 4096). The agent receives each fact's text and, when the fact carries it, when it
  happened. `reflect` (a model call per read) is not measured here.
- A live exchange (metrics 6 and 9) is one `retain` item with `async` true, its documented
  asynchronous mode: the call returns once the work is queued.
- Open (metric 8): `GET .../memories/{id}` on the first fact.
- Models: the extraction model and the embedder are the benchmark's, through its gateway (both are
  OpenAI-compatible settings); the reranker is its default, a local cross-encoder.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import Identities
from niadra_bench.systems.base import Call, HttpSystem

TURN_SPACING_S = 40


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def retain_item(case: Case, ids: Identities, session: Session, now: datetime, bank: str) -> dict[str, Any]:
    start = now - timedelta(days=session.days_ago)
    lines: list[str] = []
    if session.record is not None:
        record = ids.fill(session.record.text, case.customer.name)
        lines.append(f"{session.channel.upper()} record ({_stamp(start)}): {record}")
    for index, turn in enumerate(session.turns):
        name = case.customer.name if turn.role == "customer" else "Agent"
        lines.append(f"{name} ({_stamp(start + timedelta(seconds=TURN_SPACING_S * index))}): {turn.text}")
    kind = "record" if not session.turns else "customer service conversation"
    return {
        "content": "\n".join(lines),
        "timestamp": _stamp(start + timedelta(seconds=TURN_SPACING_S * max(0, len(session.turns) - 1))),
        "context": f"{session.channel} {kind}",
        "document_id": f"{bank}-{session.id}",
    }


def fact_line(result: dict[str, Any]) -> str:
    text = str(result.get("text") or "").strip()
    when = result.get("occurred_start") or result.get("mentioned_at")
    return f"{text} ({when})" if text and when else text


class Hindsight(HttpSystem):
    system = "hindsight"
    title = "Hindsight"
    compose = "hindsight"
    url_env = "HINDSIGHT_URL"
    default_url = "http://hindsight:8888"
    version = "0.10.1"
    has_open = True
    meter_env = "HINDSIGHT_METER_URL"
    meter_default = "http://hindsight-gateway:8081"
    # Each retain waits for its extraction.
    seed_concurrency = 4

    def __init__(self, *, now: datetime | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._now = now

    def bank(self, ids: Identities) -> str:
        return self.customer(ids)

    def _path(self, ids: Identities, rest: str) -> str:
        return f"/v1/default/banks/{self.bank(ids)}{rest}"

    def health_call(self) -> Call:
        return Call("GET", "/health")

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        now = self._now or datetime.now(UTC)
        bank = self.bank(ids)
        return [
            Call(
                "POST",
                self._path(ids, "/memories"),
                json={"items": [retain_item(case, ids, s, now, bank)], "async": False},
            )
            for s in case.chronological()
        ]

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        return Call("POST", self._path(ids, "/memories/recall"), json={"query": question})

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        return [line for r in response.json().get("results") or [] if (line := fact_line(r))]

    def open_call(self, case: Case, ids: Identities, read: httpx.Response) -> Call | None:  # noqa: ARG002
        first = next((str(r["id"]) for r in read.json().get("results") or [] if r.get("id")), None)
        return Call("GET", self._path(ids, f"/memories/{first}")) if first else None

    def exchange_call(
        self,
        case: Case,
        ids: Identities,
        conversation: str,  # noqa: ARG002
        customer: str,
        agent: str | None,
    ) -> Call:
        now = _stamp(datetime.now(UTC))
        lines = [f"{case.customer.name} ({now}): {customer}"]
        if agent:
            lines.append(f"Agent ({now}): {agent}")
        item = {
            "content": "\n".join(lines),
            "timestamp": now,
            "context": "whatsapp customer service conversation",
        }
        return Call("POST", self._path(ids, "/memories"), json={"items": [item], "async": True})

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:
        """Waits until no bank has an operation pending or processing (consolidation included)."""
        started = time.monotonic()
        pending = {self.bank(ids) for _, ids in pairs}
        by_bank = {self.bank(ids): ids for _, ids in pairs}
        rounds = 0
        while pending and time.monotonic() - started < self.settle_timeout_s:
            rounds += 1
            for bank in sorted(pending):
                busy = 0
                for status in ("pending", "processing"):
                    call = Call(
                        "GET",
                        self._path(by_bank[bank], "/operations"),
                        params={"status": status, "limit": "1"},
                    )
                    try:
                        response = await call.send(self.http, self.url, self.headers)
                        busy += int(response.json().get("total") or 0) if response.status_code == 200 else 0
                    except (httpx.HTTPError, ValueError):
                        busy += 1
                if not busy:
                    pending.discard(bank)
            if pending:
                await asyncio.sleep(5)
        return {
            "settled": not pending,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "unsettled": len(pending),
        }
