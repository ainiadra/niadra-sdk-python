"""Cognee (github.com/topoteretes/cognee, Apache 2.0), its published image at 1.6.1 with its default local
stores and its default access control (each dataset has its own stores; every call is made as its default
user, logged in at the start). Driven the way its "Minimal docker-compose" guide calls its REST API:

- Writes: one dataset per customer (Cognee has no identity across channels, so this is Mem0's
  `known_id`, its best case). Each session is one text added to it (`POST /api/v1/add`, `raw_data`): the
  channel on its first line, then the conversation as `Name: text` lines; a CRM or ERP record is a line of
  its own. Then one `cognify` of the dataset (`POST /api/v1/cognify`), which builds the graph and returns
  when it is built. The API takes no time for a text: the order of the writes is the only time it has.
- Settle: nothing is left running after `cognify` returns.
- Reads: `POST /api/v1/search` with `GRAPH_COMPLETION` (the guide's search; the server's default is now
  `HYBRID_COMPLETION`), the question, the customer's dataset and its defaults (`top_k` 15), in a session
  of that customer's own (`session_id`: without one, every search shares the default session, whose
  history feeds the next completion). Cognee answers with its model; the agent receives the answer text.
  A model call on every read (with its automatic turn analysis, a second one), which the latency, the
  cost and the tokens show.
- A live exchange (metrics 6 and 9) is one `POST /api/v1/remember` (add, then cognify and its improve
  step) with `run_in_background`, so the call returns before the graph is built; metric 6 counts that.
- Models: its language model is the benchmark's extraction model and its embedder the benchmark's, both
  through the system's gateway (deploy/systems/cognee/compose.yaml).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import httpx

from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import Identities
from niadra_bench.systems.base import Call, HttpSystem

API = "/api/v1"
DEFAULT_USER = "default_user@example.com"
SEARCH_TYPE = "GRAPH_COMPLETION"
CHANNEL_NAMES = {
    "whatsapp": "WhatsApp",
    "voice": "Phone call",
    "email": "E-mail",
    "app": "App chat",
    "crm": "CRM",
    "erp": "ERP",
}


def session_text(case: Case, ids: Identities, session: Session) -> str:
    """One session as a text: its channel, then its record and its turns, one per line."""
    kind = "record" if not session.turns else "conversation"
    lines = [f"{CHANNEL_NAMES.get(session.channel, session.channel)} {kind}"]
    if session.record is not None:
        lines.append(f"{session.channel.upper()} record: {ids.fill(session.record.text, case.customer.name)}")
    for turn in session.turns:
        lines.append(f"{case.customer.name if turn.role == 'customer' else 'Agent'}: {turn.text}")
    return "\n".join(lines)


def answer_lines(data: Any) -> list[str]:
    """Every text a search answered, in order, wherever the answer nests it."""
    out: list[str] = []
    if isinstance(data, str):
        out += [line.strip() for line in data.splitlines() if line.strip()]
    elif isinstance(data, list):
        for value in data:
            out += answer_lines(value)
    elif isinstance(data, dict):
        out += answer_lines(data.get("search_result", data.get("text", "")))
    return out


class Cognee(HttpSystem):
    system = "cognee"
    title = "Cognee"
    compose = "cognee"
    url_env = "COGNEE_URL"
    default_url = "http://cognee:8000"
    version = "1.6.1"
    meter_env = "COGNEE_METER_URL"
    meter_default = "http://cognee-gateway:8081"
    #: `add` and `cognify` wait for the graph to be built.
    seed_concurrency = 4

    def health_call(self) -> Call:
        return Call("GET", "/health")

    async def start(self) -> None:
        """Waits for the server, then logs in as its default user (access control is on, its default)."""
        await super().start()
        password = os.environ.get("COGNEE_DEFAULT_USER_PASSWORD", "bench-local-only")
        login = Call("POST", f"{API}/auth/login", form={"username": DEFAULT_USER, "password": password})
        response = await login.send(self.http, self.url, {})
        response.raise_for_status()
        self.token = str(response.json()["access_token"])

    def dataset(self, ids: Identities) -> str:
        return self.customer(ids)

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        dataset = self.dataset(ids)
        texts = [session_text(case, ids, one) for one in case.chronological()]
        adds = [Call("POST", f"{API}/add", form={"datasetName": dataset, "raw_data": [t]}) for t in texts]
        return [*adds, Call("POST", f"{API}/cognify", json={"datasets": [dataset]})]

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        dataset = self.dataset(ids)
        body = {
            "searchType": SEARCH_TYPE,
            "query": question,
            "datasets": [dataset],
            "sessionId": f"{dataset}-agent",
        }
        return Call("POST", f"{API}/search", json=body)

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        return answer_lines(response.json())

    def render(self, lines: Sequence[str]) -> str:
        return "\n".join(lines)

    def exchange_call(
        self,
        case: Case,
        ids: Identities,
        conversation: str,  # noqa: ARG002
        customer: str,
        agent: str | None,
    ) -> Call:
        lines = ["WhatsApp conversation", f"{case.customer.name}: {customer}"]
        if agent:
            lines.append(f"Agent: {agent}")
        form: dict[str, str | list[str]] = {
            "datasetName": self.dataset(ids),
            "raw_data": ["\n".join(lines)],
            "run_in_background": "true",
        }
        return Call("POST", f"{API}/remember", form=form)

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:  # noqa: ARG002
        """`cognify` returned after building each customer's graph: nothing runs in the background."""
        return {"settled": True, "seconds": 0.0}
