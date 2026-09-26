"""Memobase (github.com/memodb-io/memobase, Apache 2.0), its API server at v0.0.42 with PostgreSQL and
Redis, driven the way its Python client does it (`src/client/memobase/core/entry.py`):

- Writes: one user per customer (`POST /api/v1/users` with a UUID derived from the customer's id;
  Memobase has no identity across channels, so this is Mem0's `known_id`, its best case). Each session is
  one `ChatBlob` (`POST /api/v1/blobs/insert/{user}`), the customer's messages as `user` and the agent's
  as `assistant`, each with its `created_at`; a CRM or ERP record is a `user` message with the system as
  its alias. After each session, `flush` (`POST /api/v1/users/buffer/{user}/chat`), as its docs ask at
  the end of a session.
- Settle: the user's chat buffer is read (`GET .../buffer/capacity/{user}/chat`) until nothing is idle or
  processing.
- Reads: `context()` (`GET /api/v1/users/context/{user}`), with `max_token_size` the budget of the view
  the probe reads (600 on voice, 1,500 in chat, Niadra's own budgets) and the customer's question as the
  current chat (`chats_str`), so the events it picks are the ones near the question. The agent receives
  the context string, one line per line.
- A live exchange (metrics 6 and 9) is one insert, the client's default (no processing wait); the
  buffer flushes by itself past its size or age, and metric 6 counts that wait.
- Models: the extraction model and the embedder are the benchmark's, through its gateway
  (deploy/systems/memobase/config.yaml). Profile topics are its defaults.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import Identities
from niadra_bench.systems.base import Call, HttpSystem

API = "/api/v1"
TURN_SPACING_S = 40
#: Niadra's view budgets (tokens), the budget Memobase's context gets for the same probe.
VIEW_BUDGET = {"voice": 600, "chat": 1500}


def blob(case: Case, ids: Identities, session: Session, now: datetime) -> dict[str, Any]:
    start = now - timedelta(days=session.days_ago)
    messages: list[dict[str, Any]] = []
    if session.record is not None:
        messages.append(
            {
                "role": "user",
                "content": ids.fill(session.record.text, case.customer.name),
                "alias": f"{session.channel} system",
                "created_at": start.isoformat(),
            }
        )
    for index, turn in enumerate(session.turns):
        customer = turn.role == "customer"
        messages.append(
            {
                "role": "user" if customer else "assistant",
                "content": turn.text,
                "alias": case.customer.name if customer else "agent",
                "created_at": (start + timedelta(seconds=TURN_SPACING_S * index)).isoformat(),
            }
        )
    return {"blob_type": "chat", "fields": {"channel": session.channel}, "blob_data": {"messages": messages}}


class Memobase(HttpSystem):
    system = "memobase"
    title = "Memobase"
    compose = "memobase"
    url_env = "MEMOBASE_URL"
    default_url = "http://memobase:8000"
    token_env = "MEMOBASE_ACCESS_TOKEN"  # noqa: S105 - the variable's name, not a token
    version = "0.0.42"
    min_settle_timeout_s = 4 * 3600.0
    meter_env = "MEMOBASE_METER_URL"
    meter_default = "http://memobase-gateway:8081"

    def __init__(self, *, now: datetime | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._now = now

    def store(self, ids: Identities) -> str:
        """Memobase's users are UUIDs: one derived from the customer's id."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"niadra-bench:{self.customer(ids)}"))

    def health_call(self) -> Call:
        return Call("GET", f"{API}/healthcheck")

    def ensure_calls(self, case: Case, ids: Identities) -> list[Call]:  # noqa: ARG002
        """Memobase writes only for a user it has: the client's `add_user` with the customer's UUID."""
        return [
            Call(
                "POST", f"{API}/users", json={"data": {"customer": self.customer(ids)}, "id": self.store(ids)}
            )
        ]

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        now = self._now or datetime.now(UTC)
        user = self.store(ids)
        calls: list[Call] = []
        for session in case.chronological():
            calls.append(Call("POST", f"{API}/blobs/insert/{user}", json=blob(case, ids, session, now)))
            calls.append(Call("POST", f"{API}/users/buffer/{user}/chat", params={"wait_process": "false"}))
        return calls

    def retry_of(self, call: Call, response: httpx.Response) -> Call | None:
        # A user created by an earlier attempt answers a conflict: it exists, which is what was asked.
        if (
            call.path == f"{API}/users"
            and response.status_code in (409, 400, 500)
            and "exist" in response.text
        ):
            return None
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict) and body.get("errno", 0) not in (0, None):
            raise httpx.HTTPStatusError(
                f"errno {body.get('errno')}: {body.get('errmsg')}",
                request=response.request,
                response=response,
            )
        return None

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:
        view = "voice" if case.probe.channel == "voice" else "chat"
        params = {
            "max_token_size": str(VIEW_BUDGET[view]),
            "chats_str": json.dumps([{"role": "user", "content": question}], ensure_ascii=False),
        }
        return Call("GET", f"{API}/users/context/{self.store(ids)}", params=params)

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        context = str(((response.json() or {}).get("data") or {}).get("context") or "")
        return [line for line in context.splitlines() if line.strip()]

    def render(self, lines: Sequence[str]) -> str:
        """The context string as `context()` returns it: it is already a prompt block."""
        return "\n".join(lines)

    def exchange_call(
        self,
        case: Case,
        ids: Identities,
        conversation: str,  # noqa: ARG002
        customer: str,
        agent: str | None,
    ) -> Call:
        now = datetime.now(UTC).isoformat()
        messages = [{"role": "user", "content": customer, "alias": case.customer.name, "created_at": now}]
        if agent:
            messages.append({"role": "assistant", "content": agent, "alias": "agent", "created_at": now})
        body = {"blob_type": "chat", "fields": {"channel": "whatsapp"}, "blob_data": {"messages": messages}}
        return Call("POST", f"{API}/blobs/insert/{self.store(ids)}", json=body)

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:
        """Waits until no customer's chat buffer has a blob idle or processing."""
        started = time.monotonic()
        pending = {self.store(ids) for _, ids in pairs}
        rounds = 0
        while pending and time.monotonic() - started < self.settle_timeout_s:
            rounds += 1
            for user in sorted(pending):
                waiting = 0
                for status in ("idle", "processing"):
                    call = Call("GET", f"{API}/users/buffer/capacity/{user}/chat", params={"status": status})
                    try:
                        response = await call.send(self.http, self.url, self.headers)
                        ids = ((response.json() or {}).get("data") or {}).get("ids") or []
                        waiting += len(ids) if response.status_code == 200 else 1
                    except (httpx.HTTPError, ValueError):
                        waiting += 1
                if not waiting:
                    pending.discard(user)
            if pending:
                await asyncio.sleep(5)
        return {
            "settled": not pending,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "unsettled": len(pending),
        }
