"""LangMem (github.com/langchain-ai/langmem, MIT) 0.0.30, a library with no server of its own: the harness
runs it in a small HTTP service of its own (`deploy/systems/langmem/server.py`), which calls only the
library's documented API over LangGraph's `AsyncPostgresStore` in PostgreSQL with pgvector, as its
"Background Quickstart" and "Delayed Background Memory Processing" guides use it:

- Writes: one namespace per customer (`("memories", "{user_id}")`, the guide on dynamic namespaces;
  LangMem has no identity across channels, so this is Mem0's `known_id`, its best case). Each session
  goes to `create_memory_store_manager` (its defaults: the `Memory` schema, inserts and updates, no
  deletes) as one conversation in OpenAI's message format, in the background: the service answers once
  the conversation is queued and one worker processes the queue in order, as the library's
  `ReflectionExecutor` does. A CRM or ERP record is a `system` message. The manager takes no time for a
  message: the order of the writes is the only time it has.
- Settle: the service's queue (`GET /v1/queue`) until nothing is queued or running.
- Reads: the store's search in the customer's namespace with the question, `limit` 10 (the default of
  its `search_memory` tool, `create_search_memory_tool`). The agent receives each memory's content.
- A live exchange (metrics 6 and 9) is one conversation queued the same way; the wait in the queue is in
  metric 6.
- Open (metric 8): the store's `get` of the first memory.
- Models: the manager's model is the benchmark's extraction model and the store's embedder the
  benchmark's (384 dimensions), both through the system's gateway.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import Identities
from niadra_bench.systems.base import Call, HttpSystem


def conversation(case: Case, ids: Identities, session: Session) -> list[dict[str, str]]:
    """A session in OpenAI's message format: its record, then every turn."""
    messages: list[dict[str, str]] = []
    if session.record is not None:
        text = ids.fill(session.record.text, case.customer.name)
        messages.append({"role": "system", "content": f"{session.channel.upper()} record: {text}"})
    for turn in session.turns:
        messages.append({"role": "user" if turn.role == "customer" else "assistant", "content": turn.text})
    return messages


class LangMem(HttpSystem):
    system = "langmem"
    title = "LangMem"
    compose = "langmem"
    url_env = "LANGMEM_URL"
    default_url = "http://langmem:8000"
    version = "0.0.30"
    min_settle_timeout_s = 6 * 3600.0
    has_open = True
    meter_env = "LANGMEM_METER_URL"
    meter_default = "http://langmem-gateway:8081"

    def health_call(self) -> Call:
        return Call("GET", "/health")

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        user = self.customer(ids)
        return [
            Call(
                "POST",
                "/v1/conversations",
                json={"user_id": user, "thread_id": f"{user}-{s.id}", "messages": conversation(case, ids, s)},
            )
            for s in case.chronological()
        ]

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        return Call("POST", "/v1/search", json={"user_id": self.customer(ids), "query": question})

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        return [str(m["content"]).strip() for m in response.json().get("memories") or [] if m.get("content")]

    def open_call(self, case: Case, ids: Identities, read: httpx.Response) -> Call | None:  # noqa: ARG002
        first = next((str(m["key"]) for m in read.json().get("memories") or [] if m.get("key")), None)
        return Call("GET", f"/v1/memories/{self.customer(ids)}/{first}") if first else None

    def exchange_call(
        self,
        case: Case,  # noqa: ARG002
        ids: Identities,
        conversation: str,
        customer: str,
        agent: str | None,
    ) -> Call:
        messages = [{"role": "user", "content": customer}]
        if agent:
            messages.append({"role": "assistant", "content": agent})
        body = {"user_id": self.customer(ids), "thread_id": conversation, "messages": messages}
        return Call("POST", "/v1/conversations", json=body)

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:  # noqa: ARG002
        """Waits until the service's queue has nothing queued or running."""
        started = time.monotonic()
        rounds = 0
        left: int | None = None
        failed = 0
        while time.monotonic() - started < self.settle_timeout_s:
            rounds += 1
            try:
                response = await Call("GET", "/v1/queue").send(self.http, self.url, self.headers)
                response.raise_for_status()
                data = response.json()
                left = int(data.get("queued") or 0) + int(data.get("running") or 0)
                failed = int(data.get("failed") or 0)
            except (httpx.HTTPError, ValueError):
                left = None
            if left == 0:
                break
            await asyncio.sleep(5)
        return {
            "settled": left == 0,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "left": left,
            "failed": failed,
        }
