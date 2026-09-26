"""Graphiti (github.com/getzep/graphiti, Apache 2.0), through its own REST server (`server/`, the
`zepai/graphiti` image at 0.30.2) with Neo4j 5.26, driven the way the server's routes are meant to be
used:

- Writes: `POST /messages` with the customer's `group_id` (one group per customer: Graphiti has no
  identity across channels, so this is Mem0's `known_id`, its best case) and every message of a session,
  each with its `role_type` (`user`, `assistant`, or `system` for a CRM or ERP record), its `role`, its
  `timestamp` (the time the message was said: Graphiti takes the time of the event, its
  `reference_time`) and the channel as `source_description`. The server answers 202 and adds each message
  as an episode, one after the other, in a background queue.
- Settle: the queue has no status route, so the harness reads `GET /episodes/{group_id}` until every
  message of every customer is an episode.
- Reads: `POST /search` with the customer's group, the question and `max_facts` 10 (its default). The
  agent receives each fact with the time it holds from and until, the fields the search returns.
- Open (metric 8): `GET /entity-edge/{uuid}` on the first fact.
- Models: the server takes one OpenAI-compatible base URL for its model and its embedder; it goes to
  the system's gateway, which sends the embeddings to the benchmark's embedder and every model call to
  the benchmark's extraction model (the server fixes a smaller model for light prompts; the gateway
  makes it the same one). The search's default recipe (hybrid, RRF) calls no model.
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

MAX_FACTS = 10
TURN_SPACING_S = 40  # the same spacing Niadra's events get


def session_messages(case: Case, ids: Identities, session: Session, now: datetime) -> list[dict[str, Any]]:
    start = now - timedelta(days=session.days_ago)
    out: list[dict[str, Any]] = []
    if session.record is not None:
        out.append(
            {
                "content": ids.fill(session.record.text, case.customer.name),
                "role_type": "system",
                "role": session.channel,
                "timestamp": start.isoformat(),
                "source_description": f"{session.channel} record",
            }
        )
    for index, turn in enumerate(session.turns):
        customer = turn.role == "customer"
        out.append(
            {
                "content": turn.text,
                "role_type": "user" if customer else "assistant",
                "role": case.customer.name if customer else "agent",
                "timestamp": (start + timedelta(seconds=TURN_SPACING_S * index)).isoformat(),
                "source_description": f"{session.channel} conversation",
            }
        )
    return out


def fact_line(fact: dict[str, Any]) -> str:
    text = str(fact.get("fact") or "").strip()
    if not text:
        return ""
    since, until = fact.get("valid_at"), fact.get("invalid_at")
    if since and until:
        return f"{text} (from {since} until {until})"
    if since:
        return f"{text} (since {since})"
    return text


class Graphiti(HttpSystem):
    system = "graphiti"
    title = "Graphiti server"
    compose = "graphiti"
    url_env = "GRAPHITI_URL"
    default_url = "http://graphiti:8000"
    version = "0.30.2"
    min_settle_timeout_s = 12 * 3600.0
    has_open = True
    meter_env = "GRAPHITI_METER_URL"
    meter_default = "http://graphiti-gateway:8081"

    def __init__(self, *, now: datetime | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._now = now

    def group(self, ids: Identities) -> str:
        return self.customer(ids)

    def health_call(self) -> Call:
        return Call("GET", "/healthcheck")

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        now = self._now or datetime.now(UTC)
        calls = []
        for session in case.chronological():
            messages = session_messages(case, ids, session, now)
            if messages:
                calls.append(
                    Call("POST", "/messages", json={"group_id": self.group(ids), "messages": messages})
                )
        return calls

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        body = {"group_ids": [self.group(ids)], "query": question, "max_facts": MAX_FACTS}
        return Call("POST", "/search", json=body)

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        return [line for fact in response.json().get("facts") or [] if (line := fact_line(fact))]

    def open_call(self, case: Case, ids: Identities, read: httpx.Response) -> Call | None:  # noqa: ARG002
        facts = read.json().get("facts") or []
        uuid = next((str(f["uuid"]) for f in facts if f.get("uuid")), None)
        return Call("GET", f"/entity-edge/{uuid}") if uuid else None

    def exchange_call(
        self,
        case: Case,
        ids: Identities,
        conversation: str,  # noqa: ARG002
        customer: str,
        agent: str | None,
    ) -> Call:
        now = datetime.now(UTC).isoformat()
        messages = [{"content": customer, "role_type": "user", "role": case.customer.name, "timestamp": now}]
        if agent:
            messages.append({"content": agent, "role_type": "assistant", "role": "agent", "timestamp": now})
        return Call("POST", "/messages", json={"group_id": self.group(ids), "messages": messages})

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:
        """Waits until every message of every customer is an episode (the server's queue is drained)."""
        started = time.monotonic()
        expected = {
            self.group(ids): sum(len(s.turns) + (s.record is not None) for s in case.sessions)
            for case, ids in pairs
        }
        pending = dict(expected)
        rounds = 0
        while pending and time.monotonic() - started < self.settle_timeout_s:
            rounds += 1
            for group, want in list(pending.items()):
                call = Call("GET", f"/episodes/{group}", params={"last_n": str(want + 5)})
                try:
                    response = await call.send(self.http, self.url, self.headers)
                    if response.status_code == 200 and len(response.json()) >= want:
                        del pending[group]
                except (httpx.HTTPError, ValueError):
                    pass
            if pending:
                await asyncio.sleep(5)
        return {
            "settled": not pending,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "unsettled": len(pending),
        }
