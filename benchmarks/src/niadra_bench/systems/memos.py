"""MemOS (github.com/MemTensor/MemOS, Apache 2.0) at v2.0.34, its server API (`memos.api.server_api`,
routes under `/product`) with Neo4j 5.26 and Qdrant 1.15, as its docker/docker-compose.yml runs it, driven
the way the route models document it (`src/memos/api/product_models.py`):

- Writes: `POST /product/add` per exchange (the customer's message and the agent's answer), with the
  customer's `user_id` and cube (one user and one cube per customer: MemOS has no identity across
  channels, so this is Mem0's `known_id`, its best case), the session as `session_id`, each message with
  its `chat_time`, and `async_mode` `async`, its default. A CRM or ERP record is a `system` message.
- Settle: `POST /product/scheduler/wait` for each customer, as its docs ask before reading; then, since the
  scheduler's task tracking needs the optional Redis queue (off in its example configuration), the
  customer's memory count (`POST /product/get_all`) must also stay the same for a while.
- Reads: `POST /product/search` with the question, the customer's user and cube and its defaults (`fast`
  mode, `top_k` 10). The agent receives each memory's text.
- Open (metric 8): `GET /product/get_memory/{memory_id}` on the first memory.
- Models: every model setting and the embedder are the benchmark's, through the system's gateway; the
  reranker is `cosine_local`, its example's. The activation memory (a KV cache of a local model) is not
  part of the server API and is not measured.
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
from niadra_bench.targets.mem0 import exchanges

API = "/product"
TURN_SPACING_S = 40
QUIET_S = 20.0


def _time(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M:%S")


def session_adds(
    case: Case, ids: Identities, session: Session, now: datetime, user: str
) -> list[dict[str, Any]]:
    """The add() calls of one session: its record, then one per exchange."""
    start = now - timedelta(days=session.days_ago)
    base = {
        "user_id": user,
        "writable_cube_ids": [user],
        "session_id": f"{user}-{session.id}",
        "async_mode": "async",
    }
    out: list[dict[str, Any]] = []
    if session.record is not None:
        text = ids.fill(session.record.text, case.customer.name)
        out.append({**base, "messages": [{"role": "system", "content": text, "chat_time": _time(start)}]})
    index = 0
    for group in exchanges(session):
        messages = []
        for message in group:
            messages.append(
                {**message, "chat_time": _time(start + timedelta(seconds=TURN_SPACING_S * index))}
            )
            index += 1
        out.append({**base, "messages": messages})
    return out


def found_memories(data: Any) -> list[dict[str, Any]]:
    """Every memory item in a search answer, in order, wherever the answer nests it."""
    out: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if isinstance(data.get("memory"), str):
            return [data]
        for value in data.values():
            out += found_memories(value)
    elif isinstance(data, list):
        for value in data:
            out += found_memories(value)
    return out


class MemOS(HttpSystem):
    system = "memos"
    title = "MemOS"
    compose = "memos"
    url_env = "MEMOS_URL"
    default_url = "http://memos:8000"
    version = "v2.0.34"
    has_open = True
    meter_env = "MEMOS_METER_URL"
    meter_default = "http://memos-gateway:8081"

    def __init__(self, *, now: datetime | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._now = now

    def user(self, ids: Identities) -> str:
        return self.customer(ids)

    def health_call(self) -> Call:
        return Call("GET", "/docs")

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        now = self._now or datetime.now(UTC)
        user = self.user(ids)
        return [
            Call("POST", f"{API}/add", json=body)
            for session in case.chronological()
            for body in session_adds(case, ids, session, now, user)
        ]

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        user = self.user(ids)
        return Call(
            "POST", f"{API}/search", json={"query": question, "user_id": user, "readable_cube_ids": [user]}
        )

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        return [
            m["memory"].strip() for m in found_memories(response.json().get("data")) if m["memory"].strip()
        ]

    def open_call(self, case: Case, ids: Identities, read: httpx.Response) -> Call | None:  # noqa: ARG002
        first = next((str(m["id"]) for m in found_memories(read.json().get("data")) if m.get("id")), None)
        return Call("GET", f"{API}/get_memory/{first}") if first else None

    def exchange_call(
        self,
        case: Case,  # noqa: ARG002
        ids: Identities,
        conversation: str,
        customer: str,
        agent: str | None,
    ) -> Call:
        user = self.user(ids)
        now = _time(datetime.now(UTC))
        messages = [{"role": "user", "content": customer, "chat_time": now}]
        if agent:
            messages.append({"role": "assistant", "content": agent, "chat_time": now})
        body = {
            "user_id": user,
            "writable_cube_ids": [user],
            "session_id": conversation,
            "async_mode": "async",
            "messages": messages,
        }
        return Call("POST", f"{API}/add", json=body)

    async def _count(self, user: str) -> int | None:
        call = Call("POST", f"{API}/get_all", json={"user_id": user, "memory_type": "text_mem"})
        try:
            response = await call.send(self.http, self.url, self.headers)
            response.raise_for_status()
            return len(found_memories(response.json().get("data")))
        except (httpx.HTTPError, ValueError):
            return None

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:
        """The scheduler's wait for every customer, then every customer's memory count stable for
        `QUIET_S` (the wait needs the optional Redis queue to see the tasks)."""
        started = time.monotonic()
        users = [self.user(ids) for _, ids in pairs]
        timed_out = 0
        for user in users:
            call = Call("POST", f"{API}/scheduler/wait", params={"user_name": user, "timeout_seconds": "600"})
            try:
                response = await call.send(self.http, self.url, self.headers)
                timed_out += bool(((response.json() or {}).get("data") or {}).get("timed_out"))
            except (httpx.HTTPError, ValueError):
                timed_out += 1
        last: dict[str, int | None] = {}
        quiet_since = time.monotonic()
        rounds = 0
        while time.monotonic() - started < self.settle_timeout_s:
            rounds += 1
            counts = {user: await self._count(user) for user in users}
            if counts != last:
                last, quiet_since = counts, time.monotonic()
            elif time.monotonic() - quiet_since >= QUIET_S:
                break
            await asyncio.sleep(5)
        return {
            "settled": time.monotonic() - quiet_since >= QUIET_S,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "scheduler_timeouts": timed_out,
            "memories": sum(c or 0 for c in last.values()),
        }
