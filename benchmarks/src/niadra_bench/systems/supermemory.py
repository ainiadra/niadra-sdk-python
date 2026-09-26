"""Supermemory local (github.com/supermemoryai/supermemory, the MIT `supermemory-server` binary at
server-v0.0.8), driven the way its quickstart's harness does it (apps/docs/quickstart.mdx, "Put it in a
harness"):

- Writes: one document per session (`POST /v3/documents`) with the conversation as `user:` and
  `assistant:` lines, the customer's `containerTag` (Supermemory has no identity across channels, so this
  is Mem0's `known_id`, its best case), the session as `customId` ("one session, one document") and
  `dreaming: "instant"`, which its docs name for benchmarking. A CRM or ERP record is its own document.
  The API takes no time for an event: the order of the writes is the only time it has.
- Settle: the customer's documents are listed (`POST /v3/documents/list`) until each is `done` (or
  `failed`).
- Reads: `POST /v4/profile` with the customer's tag and the question as `q`, and the context the
  quickstart builds from it: the static profile, the dynamic profile and the related memories, under their
  headings. (The optional document chunks of that example are left out: the dataset has no documents.)
- A live exchange (metrics 6 and 9) is one document with the two lines, the documented `add`.
- Models: its model and its embedder are the benchmark's, through the system's gateway. The local binary
  signs requests from localhost with its key, so the harness reaches it through a forwarder that shares its
  network namespace (`supermemory-local`, in the compose file): the key never leaves the container.
- The local binary is licensed for 10,000 documents: a run writes about 2,800 a repetition of dataset v2,
  plus the timed writes; run one repetition per fresh container when more are needed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import httpx

from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import Identities
from niadra_bench.systems.base import Call, HttpSystem

DONE = frozenset({"done", "failed"})


def conversation(case: Case, ids: Identities, session: Session) -> str:
    lines = []
    if session.record is not None:
        lines.append(f"{session.channel} record: {ids.fill(session.record.text, case.customer.name)}")
    for turn in session.turns:
        lines.append(f"{'user' if turn.role == 'customer' else 'assistant'}: {turn.text}")
    return "\n".join(lines)


class Supermemory(HttpSystem):
    system = "supermemory"
    title = "Supermemory local"
    compose = "supermemory"
    url_env = "SUPERMEMORY_URL"
    default_url = "http://supermemory:6768"
    version = "server-v0.0.8"
    min_settle_timeout_s = 4 * 3600.0
    meter_env = "SUPERMEMORY_METER_URL"
    meter_default = "http://supermemory-gateway:8081"

    def tag(self, ids: Identities) -> str:
        return self.customer(ids)

    def health_call(self) -> Call:
        return Call("POST", "/v3/documents/list", json={"containerTags": ["health"], "limit": 1})

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        tag = self.tag(ids)
        return [
            Call(
                "POST",
                "/v3/documents",
                json={
                    "content": conversation(case, ids, s),
                    "containerTag": tag,
                    "customId": f"{tag}-{s.id}",
                    "metadata": {"type": "conversation" if s.turns else "record", "channel": s.channel},
                    "dreaming": "instant",
                },
            )
            for s in case.chronological()
        ]

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        return Call("POST", "/v4/profile", json={"containerTag": self.tag(ids), "q": question})

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        data = response.json()
        profile = data.get("profile") or {}
        related = [str(r.get("memory") or "") for r in (data.get("searchResults") or {}).get("results") or []]
        out: list[str] = []
        for heading, items in (
            ("## Profile (static)", profile.get("static") or []),
            ("## Profile (dynamic)", profile.get("dynamic") or []),
            ("## Related memories", related),
        ):
            items = [str(i).strip() for i in items if str(i).strip()]
            if items:
                out += [heading, *(f"- {i}" for i in items)]
        return out

    def render(self, lines: Sequence[str]) -> str:
        return "\n".join(lines)

    def exchange_call(
        self,
        case: Case,  # noqa: ARG002
        ids: Identities,
        conversation: str,
        customer: str,
        agent: str | None,
    ) -> Call:
        content = f"user: {customer}" + (f"\nassistant: {agent}" if agent else "")
        tag = self.tag(ids)
        body = {
            "content": content,
            "containerTag": tag,
            "customId": f"{tag}-{conversation}",
            "dreaming": "instant",
        }
        return Call("POST", "/v3/documents", json=body)

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:
        """Waits until every document of every customer is `done` (or `failed`)."""
        started = time.monotonic()
        expected = {self.tag(ids): len(case.sessions) for case, ids in pairs}
        pending = dict(expected)
        failed = 0
        rounds = 0
        while pending and time.monotonic() - started < self.settle_timeout_s:
            rounds += 1
            for tag, want in list(pending.items()):
                body = {"containerTags": [tag], "limit": max(100, want)}
                try:
                    response = await Call("POST", "/v3/documents/list", json=body).send(
                        self.http, self.url, self.headers
                    )
                    docs = response.json().get("memories") or [] if response.status_code == 200 else []
                except (httpx.HTTPError, ValueError):
                    continue
                statuses = [str(d.get("status")) for d in docs]
                if len(statuses) >= want and all(s in DONE for s in statuses):
                    failed += statuses.count("failed")
                    del pending[tag]
            if pending:
                await asyncio.sleep(5)
        return {
            "settled": not pending,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "unsettled": len(pending),
            "failed_documents": failed,
        }
