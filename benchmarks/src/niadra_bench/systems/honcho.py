"""Honcho (github.com/plastic-labs/honcho, AGPL 3.0), its published image at v3.2.1
(`ghcr.io/plastic-labs/honcho`: the API and the deriver) with PostgreSQL and pgvector and Redis, as its
docker-compose.yml.example runs it. The harness only runs it and calls its REST API: no line of Honcho is
in this repository.

Driven the way its documentation shows (quickstart, "Get Context", "Design Patterns", "Queue Status"):

- Writes: one workspace for the run; one peer per customer (Honcho has no identity across channels, so
  this is Mem0's `known_id`, its best case) and one peer for the company's agents and one for its systems
  of record, both with `observe_me` false, as its design patterns ask for peers it does not need to model.
  Each session is one session (`POST .../sessions/{id}/messages`, which creates it with its peers), each
  message with its peer and its `created_at`; a CRM or ERP record is a message of the systems peer.
- Settle: the workspace's queue (`GET .../queue/status`) until no work unit is pending or in progress. Its
  deriver waits until a work unit has 512 tokens, or for 30 minutes (its defaults), before it reasons.
- Reads, two columns on the same server:
  - `honcho`: the customer peer's context (`GET .../peers/{id}/context`, "representation and peer card in
    one call") with the question as `search_query` and its defaults. The agent receives the peer card,
    then the representation. No model call on the read.
  - `honcho_dialectic`: the dialectic API (`POST .../peers/{id}/chat`, the quickstart's `peer.chat()`)
    with the question and its defaults (`reasoning_level` low). The agent receives the answer text. A
    model call on every read, which the latency, the cost and the tokens show.
- A live exchange (metrics 6 and 9) is one message batch; the deriver's wait is in metric 6.
- Models: every model setting (deriver, summary, dialectic levels, dreams) is the benchmark's extraction
  model and the embedder the benchmark's (384 dimensions, the pgvector columns set to it at bootstrap
  with its `configure_embeddings.py`, as its configuration docs ask), through the system's gateway.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import Identities
from niadra_bench.systems.base import Call, HttpSystem

TURN_SPACING_S = 40
AGENT_PEER = "company-agent"
RECORDS_PEER = "company-systems"


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def session_messages(
    case: Case, ids: Identities, session: Session, now: datetime, peer: str
) -> list[dict[str, Any]]:
    """One session's messages, each with its peer, its time and its channel."""
    start = now - timedelta(days=session.days_ago)
    meta = {"channel": session.channel}
    out: list[dict[str, Any]] = []
    if session.record is not None:
        text = ids.fill(session.record.text, case.customer.name)
        out.append({"peer_id": RECORDS_PEER, "content": text, "created_at": _stamp(start), "metadata": meta})
    for index, turn in enumerate(session.turns):
        out.append(
            {
                "peer_id": peer if turn.role == "customer" else AGENT_PEER,
                "content": turn.text,
                "created_at": _stamp(start + timedelta(seconds=TURN_SPACING_S * index)),
                "metadata": meta,
            }
        )
    return out


def context_lines(data: dict[str, Any]) -> list[str]:
    """A peer context as the agent receives it: the peer card, then the representation."""
    lines: list[str] = []
    card = [str(item).strip() for item in data.get("peer_card") or [] if str(item).strip()]
    if card:
        lines += ["## Peer card", *card]
    representation = [line for line in str(data.get("representation") or "").splitlines() if line.strip()]
    if representation:
        lines += ["## Representation", *representation]
    return lines


class Honcho(HttpSystem):
    system = "honcho"
    title = "Honcho"
    compose = "honcho"
    url_env = "HONCHO_URL"
    default_url = "http://honcho:8000"
    version = "v3.2.1"
    min_settle_timeout_s = 4 * 3600.0
    meter_env = "HONCHO_METER_URL"
    meter_default = "http://honcho-gateway:8081"
    #: One workspace per column, so two columns never share a customer's peer.
    workspace = "niadra-bench"

    def __init__(self, *, now: datetime | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._now = now

    def _ws(self, rest: str = "") -> str:
        return f"/v3/workspaces/{self.workspace}{rest}"

    def session_id(self, ids: Identities, session: str) -> str:
        return f"{self.customer(ids)}-{session}"

    def health_call(self) -> Call:
        return Call("GET", "/health")

    def ensure_calls(self, case: Case, ids: Identities) -> list[Call]:  # noqa: ARG002
        """The workspace, the customer's peer and the company's two peers (get or create, each time)."""
        quiet = {"observe_me": False}
        return [
            Call("POST", "/v3/workspaces", json={"id": self.workspace}),
            Call("POST", self._ws("/peers"), json={"id": self.customer(ids)}),
            Call("POST", self._ws("/peers"), json={"id": AGENT_PEER, "configuration": quiet}),
            Call("POST", self._ws("/peers"), json={"id": RECORDS_PEER, "configuration": quiet}),
        ]

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        now = self._now or datetime.now(UTC)
        peer = self.customer(ids)
        return [
            Call(
                "POST",
                self._ws(f"/sessions/{self.session_id(ids, s.id)}/messages"),
                json={"messages": session_messages(case, ids, s, now, peer)},
            )
            for s in case.chronological()
        ]

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        return Call(
            "GET", self._ws(f"/peers/{self.customer(ids)}/context"), params={"search_query": question}
        )

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        return context_lines(response.json() or {})

    def render(self, lines: Sequence[str]) -> str:
        return "\n".join(line if line.startswith("## ") else f"- {line}" for line in lines)

    def exchange_call(
        self,
        case: Case,  # noqa: ARG002
        ids: Identities,
        conversation: str,
        customer: str,
        agent: str | None,
    ) -> Call:
        now = _stamp(datetime.now(UTC))
        meta = {"channel": "whatsapp"}
        messages = [{"peer_id": self.customer(ids), "content": customer, "created_at": now, "metadata": meta}]
        if agent:
            messages.append({"peer_id": AGENT_PEER, "content": agent, "created_at": now, "metadata": meta})
        path = self._ws(f"/sessions/{self.session_id(ids, conversation)}/messages")
        return Call("POST", path, json={"messages": messages})

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:  # noqa: ARG002
        """Waits until the workspace's queue has no work unit pending or in progress."""
        started = time.monotonic()
        rounds = 0
        busy: int | None = None
        status = Call("GET", self._ws("/queue/status"))
        while time.monotonic() - started < self.settle_timeout_s:
            rounds += 1
            try:
                response = await status.send(self.http, self.url, self.headers)
                response.raise_for_status()
                data = response.json()
                busy = int(data.get("pending_work_units") or 0) + int(data.get("in_progress_work_units") or 0)
            except (httpx.HTTPError, ValueError):
                busy = None
            if busy == 0:
                break
            await asyncio.sleep(10)
        return {
            "settled": busy == 0,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "work_units_left": busy,
        }


class HonchoDialectic(Honcho):
    """The same server and the same writes, in a workspace of its own, read through the dialectic API
    (`POST .../peers/{id}/chat` with the question and its defaults): Honcho reasons over what it knows of
    the customer with its model on every read and answers in text, which the agent receives."""

    system = "honcho_dialectic"
    title = "Honcho (dialectic)"
    workspace = "niadra-bench-dialectic"

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        return Call("POST", self._ws(f"/peers/{self.customer(ids)}/chat"), json={"query": question})

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        text = str((response.json() or {}).get("content") or "").strip()
        return [line for line in text.splitlines() if line.strip()]

    def render(self, lines: Sequence[str]) -> str:
        return "\n".join(lines)
