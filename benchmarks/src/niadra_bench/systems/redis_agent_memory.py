"""Redis Agent Memory Server (github.com/redis/agent-memory-server, Apache 2.0), the open server its
repository now keeps under `V0/` as a research artifact (Redis's supported product, Agent Memory in Redis
Iris, runs only in its cloud). Its all-in-one image at 0.15.2 (`redislabs/agent-memory-server:
0.15.2-standalone`: Redis 8, the API and the task worker), driven the way its "Memory Integration
Patterns" document (pattern 3, background extraction) and its REST API do it:

- Writes: one working memory per conversation (`PUT /v1/working-memory/{session_id}`), with the
  customer's `user_id` (one user per customer: the server has no identity across channels, so this is
  Mem0's `known_id`, its best case), every message of the session with its role and its `created_at`,
  and nothing else: the server promotes the messages to long-term memory in the background with its
  default strategy (`discrete`), after its debounce (30 s). A CRM or ERP record is a `system` message.
  Its extraction prompt grounds relative dates on the time the extraction runs, not on the messages'.
- Settle: each seeded conversation's working memory is read until every message is marked extracted
  (`discrete_memory_extracted`), then every customer's long-term memory count must stay the same for a
  while (Redis's own benchmark harness waits for a stable count).
- Reads: `POST /v1/long-term-memory/search` with the question as `text` and the customer's `user_id`,
  and its defaults (semantic search, `limit` 10, its recency boost). The agent receives each memory's
  text, with its event date when the extraction set one.
- A live exchange (metrics 6 and 9) is a working memory with the exchange, the documented write; the
  extraction waits for the debounce, which metric 6 counts.
- Open (metric 8): `GET /v1/long-term-memory/{id}` on the first memory.
- Models: its generation, fast and slow models are the benchmark's extraction model and its embedder the
  benchmark's, through the system's gateway (LiteLLM with `OPENAI_API_BASE`). Topic and entity extraction
  run as by default (through the same model).
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
QUIET_S = 20.0


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def working_memory(case: Case, ids: Identities, session: Session, now: datetime, user: str) -> dict[str, Any]:
    """The working memory of one session: its record, then every turn, each with its time."""
    start = now - timedelta(days=session.days_ago)
    messages: list[dict[str, str]] = []
    if session.record is not None:
        text = ids.fill(session.record.text, case.customer.name)
        messages.append({"role": "system", "content": text, "created_at": _stamp(start)})
    for index, turn in enumerate(session.turns):
        messages.append(
            {
                "role": "user" if turn.role == "customer" else "assistant",
                "content": turn.text,
                "created_at": _stamp(start + timedelta(seconds=TURN_SPACING_S * index)),
            }
        )
    return {"user_id": user, "messages": messages}


def memory_line(record: dict[str, Any]) -> str:
    text = str(record.get("text") or "").strip()
    when = record.get("event_date")
    return f"{text} (event date: {when})" if text and when else text


class RedisAgentMemory(HttpSystem):
    system = "redis_agent_memory"
    title = "Redis Agent Memory Server"
    compose = "redis-agent-memory"
    url_env = "REDIS_AGENT_MEMORY_URL"
    default_url = "http://redis-agent-memory:8000"
    version = "0.15.2"
    min_settle_timeout_s = 4 * 3600.0
    has_open = True
    meter_env = "REDIS_AGENT_MEMORY_METER_URL"
    meter_default = "http://redis-agent-memory-gateway:8081"

    def __init__(self, *, now: datetime | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._now = now

    def session_id(self, ids: Identities, session: str) -> str:
        return f"{self.customer(ids)}-{session}"

    def health_call(self) -> Call:
        return Call("GET", "/v1/health")

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        now = self._now or datetime.now(UTC)
        user = self.customer(ids)
        return [
            Call(
                "PUT",
                f"/v1/working-memory/{self.session_id(ids, s.id)}",
                json=working_memory(case, ids, s, now, user),
            )
            for s in case.chronological()
        ]

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        body = {"text": question, "user_id": {"eq": self.customer(ids)}}
        return Call("POST", "/v1/long-term-memory/search", json=body)

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        return [line for m in response.json().get("memories") or [] if (line := memory_line(m))]

    def open_call(self, case: Case, ids: Identities, read: httpx.Response) -> Call | None:  # noqa: ARG002
        first = next((str(m["id"]) for m in read.json().get("memories") or [] if m.get("id")), None)
        return Call("GET", f"/v1/long-term-memory/{first}") if first else None

    def exchange_call(
        self,
        case: Case,  # noqa: ARG002
        ids: Identities,
        conversation: str,
        customer: str,
        agent: str | None,
    ) -> Call:
        now = _stamp(datetime.now(UTC))
        messages = [{"role": "user", "content": customer, "created_at": now}]
        if agent:
            messages.append({"role": "assistant", "content": agent, "created_at": now})
        body = {"user_id": self.customer(ids), "messages": messages}
        return Call("PUT", f"/v1/working-memory/{self.session_id(ids, conversation)}", json=body)

    async def _extracted(self, session: str, user: str) -> bool:
        call = Call("GET", f"/v1/working-memory/{session}", params={"user_id": user})
        try:
            response = await call.send(self.http, self.url, self.headers)
            if response.status_code != 200:
                return False
            messages = response.json().get("messages") or []
        except (httpx.HTTPError, ValueError):
            return False
        return all(m.get("discrete_memory_extracted") == "t" for m in messages)

    async def _count(self, user: str) -> int | None:
        # No text: the server lists the customer's memories without a semantic search.
        call = Call("POST", "/v1/long-term-memory/search", json={"user_id": {"eq": user}, "limit": 100})
        try:
            response = await call.send(self.http, self.url, self.headers)
            response.raise_for_status()
            data = response.json()
            return max(int(data.get("total") or 0), len(data.get("memories") or []))
        except (httpx.HTTPError, ValueError):
            return None

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:
        """Every seeded conversation extracted, then every customer's memory count stable for `QUIET_S`."""
        started = time.monotonic()
        pending = {
            (self.session_id(ids, s.id), self.customer(ids)) for case, ids in pairs for s in case.sessions
        }
        rounds = 0
        while pending and time.monotonic() - started < self.settle_timeout_s:
            rounds += 1
            for session, user in sorted(pending):
                if await self._extracted(session, user):
                    pending.discard((session, user))
            if pending:
                await asyncio.sleep(5)
        users = sorted({self.customer(ids) for _, ids in pairs})
        last: dict[str, int | None] = {}
        quiet_since = time.monotonic()
        while time.monotonic() - started < self.settle_timeout_s:
            counts = {user: await self._count(user) for user in users}
            if counts != last:
                last, quiet_since = counts, time.monotonic()
            elif time.monotonic() - quiet_since >= QUIET_S:
                break
            await asyncio.sleep(5)
        return {
            "settled": not pending and time.monotonic() - quiet_since >= QUIET_S,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "unextracted_sessions": len(pending),
            "memories": sum(c or 0 for c in last.values()),
        }
