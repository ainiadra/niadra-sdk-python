"""ai-memory (github.com/akitaonrails/ai-memory, MIT, Rust), driven the way its own LongMemEval harness
drives it (`evals/src/retrieval/ingest.rs` and `query.rs` at tag v2.4.1):

- Writes: each session is replayed through `POST /hook/batch` at the production hook cadence,
  `session-start`, then a `user-prompt-submit` per customer turn and a `stop` carrying the opt-in
  assistant excerpt (capped at 2,000 bytes, as the client must) per agent turn, then `session-end`.
  Each session's date is prepended to its turn text (`[session date: ...]`), as their harness does for
  a replayed history. A system record (CRM, ERP, billing agent) is its own session with the record's
  text as the prompt. Items the server skipped (`accepted_indices`) are sent again.
- Identity: ai-memory has no user identity across channels, so the harness keeps one store per
  customer, a project (`workspace` `niadra-bench`, project = the customer's id), exactly as their
  harness keeps one project per question. That is Mem0's `known_id` scenario, its best case.
- Reads: the MCP tool `memory_query` over `POST /mcp` (`tools/call`) with the workspace, the project,
  the customer's question as the query and `limit` 10 (its default, and Mem0's `top_k`). The agent
  receives each hit's title and snippet, the context their harness counts and hands to its answering
  model (`query.rs`, `flatten`): page hits, then the raw-observation fallback hits.
- Open (metric 8): `memory_read_page` on the path of the first page hit.
- Configuration: the server's defaults (no LLM provider, so no consolidation model), with its optional
  local embeddings on (`AI_MEMORY_EMBEDDING_PROVIDER=local`, all-MiniLM-L6-v2 in process) and assistant
  capture on (`AI_MEMORY_CAPTURE_ASSISTANT=true`), the two settings their harness sets. Nothing tuned.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import Identities
from niadra_bench.systems.base import Call, HttpSystem

WORKSPACE = "niadra-bench"
AGENT = "claude-code"
MAX_BATCH = 256  # the server's cap on items per /hook/batch
EXCERPT_MAX_BYTES = 2_000
LIMIT = 10
MCP_ACCEPT = "application/json, text/event-stream"


def cap_excerpt(text: str) -> str:
    """The first 2,000 bytes, cut on a character boundary (their `cap_excerpt`)."""
    raw = text.encode()
    if len(raw) <= EXCERPT_MAX_BYTES:
        return text
    return raw[:EXCERPT_MAX_BYTES].decode(errors="ignore")


def session_date(now: datetime, days_ago: float) -> str:
    """The date format of their LongMemEval replay: `2023/05/20 (Sat) 02:21`."""
    return f"{now - timedelta(days=days_ago):%Y/%m/%d (%a) %H:%M}"


def hook_item(project: str, session_id: str, event: str, body: dict[str, Any]) -> dict[str, Any]:
    query = {
        "event": event,
        "agent": AGENT,
        "workspace": WORKSPACE,
        "project": project,
        "session_id": session_id,
    }
    if event == "stop":
        query["capture_assistant"] = "1"
    return {
        "url": f"/hook?{urlencode(query)}",
        "body": {"session_id": session_id, "cwd": f"/{WORKSPACE}/{project}", **body},
    }


def prompt(text: str) -> dict[str, Any]:
    return {"hook_event_name": "UserPromptSubmit", "prompt": text}


def excerpt(text: str) -> dict[str, Any]:
    return {"hook_event_name": "Stop", "_ai_memory_assistant": {"version": 1, "excerpt": cap_excerpt(text)}}


def session_items(
    case: Case, ids: Identities, project: str, session: Session, now: datetime
) -> list[dict[str, Any]]:
    """One dataset session as hook events, with its date before each text."""
    sid = f"{project}-{session.id}"
    stamp = f"[session date: {session_date(now, session.days_ago)}]"
    items = [
        hook_item(project, sid, "session-start", {"hook_event_name": "SessionStart", "source": "startup"})
    ]
    if session.record is not None:
        items.append(
            hook_item(
                project,
                sid,
                "user-prompt-submit",
                prompt(f"{stamp} {ids.fill(session.record.text, case.customer.name)}"),
            )
        )
    for turn in session.turns:
        if turn.role == "customer":
            items.append(hook_item(project, sid, "user-prompt-submit", prompt(f"{stamp} {turn.text}")))
        else:
            items.append(hook_item(project, sid, "stop", excerpt(f"{stamp} {turn.text}")))
    items.append(hook_item(project, sid, "session-end", {"hook_event_name": "SessionEnd", "reason": "exit"}))
    return items


def mcp(tool: str, arguments: dict[str, Any]) -> Call:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    return Call("POST", "/mcp", json=body, headers={"accept": MCP_ACCEPT})


def tool_result(response: httpx.Response) -> dict[str, Any]:
    """The JSON a tool returned, from a JSON or an event-stream answer (their `memory_query`)."""
    body = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        frames = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        rpc = next(json.loads(f) for f in reversed(frames) if f and "id" in json.loads(f))
    else:
        rpc = json.loads(body)
    if "error" in rpc:
        raise ValueError(f"MCP error: {rpc['error']}")
    result = rpc["result"]
    if result.get("isError"):
        raise ValueError(f"tool error: {str(result)[:200]}")
    data: dict[str, Any] = json.loads(result["content"][0]["text"])
    return data


class AiMemory(HttpSystem):
    system = "ai_memory"
    title = "ai-memory"
    compose = "ai-memory"
    url_env = "AI_MEMORY_URL"
    default_url = "http://ai-memory:49374"
    token_env = "AI_MEMORY_AUTH_TOKEN"  # noqa: S105 - the variable's name, not a token
    version = "2.4.1"
    min_settle_timeout_s = 2 * 3600.0
    has_open = True
    # The server holds one writer; its batches commit item by item.
    seed_concurrency = 4
    #: Whether the server consolidates sessions with a model after `session-end` (the `_llm` variant).
    consolidates = False
    #: How long the server must stay without a model call before its consolidation counts as done.
    CONSOLIDATION_QUIET_S = 30.0

    def __init__(self, *, now: datetime | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._now = now

    def project(self, ids: Identities) -> str:
        return self.customer(ids)

    def health_call(self) -> Call:
        return mcp("memory_status", {})

    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        now = self._now or datetime.now(UTC)
        project = self.project(ids)
        items = [
            item
            for session in case.chronological()
            for item in session_items(case, ids, project, session, now)
        ]
        return [
            Call("POST", "/hook/batch", json=items[i : i + MAX_BATCH])
            for i in range(0, len(items), MAX_BATCH)
        ]

    def retry_of(self, call: Call, response: httpx.Response) -> Call | None:
        response.raise_for_status()
        ack = response.json()
        items = list(call.json)
        if isinstance(ack.get("accepted_indices"), list):
            taken = {int(i) for i in ack["accepted_indices"]}
        else:
            taken = set(range(int(ack.get("accepted") or 0)))
        rest = [item for i, item in enumerate(items) if i not in taken]
        return Call(call.method, call.path, json=rest) if rest else None

    def read_call(self, case: Case, ids: Identities, question: str) -> Call:  # noqa: ARG002
        arguments = {"query": question, "workspace": WORKSPACE, "project": self.project(ids), "limit": LIMIT}
        return mcp("memory_query", arguments)

    def memories(self, response: httpx.Response) -> list[str]:
        response.raise_for_status()
        data = tool_result(response)
        lines = []
        for hit in [*(data.get("hits") or []), *(data.get("raw_hits") or [])]:
            title, snippet = str(hit.get("title") or "").strip(), str(hit.get("snippet") or "").strip()
            lines.append(f"{title}: {snippet}" if title and snippet else title or snippet)
        return [line for line in lines if line]

    def open_call(self, case: Case, ids: Identities, read: httpx.Response) -> Call | None:  # noqa: ARG002
        hits = tool_result(read).get("hits") or []
        path = next((str(h["path"]) for h in hits if h.get("path")), None)
        if path is None:
            return None
        return mcp("memory_read_page", {"path": path, "workspace": WORKSPACE, "project": self.project(ids)})

    def exchange_call(
        self,
        case: Case,  # noqa: ARG002
        ids: Identities,
        conversation: str,
        customer: str,
        agent: str | None,
    ) -> Call:
        """A live exchange as the hooks send it mid-session: the prompt, and the answer's excerpt.
        No date prefix: a live install sees the wall clock."""
        project = self.project(ids)
        items = [hook_item(project, conversation, "user-prompt-submit", prompt(customer))]
        if agent:
            items.append(hook_item(project, conversation, "stop", excerpt(agent)))
        return Call("POST", "/hook/batch", json=items)

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:
        """Waits until every customer's project shows every session it was sent and a page for each
        (the server writes session pages in the background after `session-end`)."""
        started = time.monotonic()
        expected = {self.project(ids): len(case.sessions) for case, ids in pairs}
        pending = set(expected)
        rounds = 0
        while pending:
            rounds += 1
            done = set()
            for project in sorted(pending):
                counts = await self._counts(project)
                want = expected[project]
                if counts.get("sessions", 0) >= want and counts.get("pages_latest", 0) >= want:
                    done.add(project)
            pending -= done
            if not pending:
                break
            if time.monotonic() - started > self.settle_timeout_s:
                break
            await asyncio.sleep(2)
        consolidation = await self._consolidated(started) if self.consolidates else None
        return {
            "settled": not pending and consolidation is not False,
            "seconds": round(time.monotonic() - started, 1),
            "rounds": rounds,
            "unsettled": len(pending),
            **({"consolidated": consolidation} if consolidation is not None else {}),
        }

    async def _consolidated(self, started: float) -> bool:
        """Waits until the durable consolidation queue has drained: no model call for
        `CONSOLIDATION_QUIET_S` and an empty write queue (`/admin/status`)."""
        while time.monotonic() - started < self.settle_timeout_s:
            try:
                response = await self.http.get(f"{self.url}/admin/status", headers=self.headers)
                response.raise_for_status()
                status = response.json()
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(5)
                continue
            llm = (status.get("providers") or {}).get("llm") or {}
            last = llm.get("last_call_at")
            queue = (status.get("write_queue") or [0])[0]
            if last and not queue:
                age = (
                    datetime.now(UTC) - datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                ).total_seconds()
                if age >= self.CONSOLIDATION_QUIET_S:
                    return True
            await asyncio.sleep(5)
        return False

    async def _counts(self, project: str) -> dict[str, int]:
        call = mcp("memory_briefing", {"workspace": WORKSPACE, "project": project})
        try:
            response = await call.send(self.http, self.url, self.headers)
            response.raise_for_status()
            counts = tool_result(response).get("counts") or {}
        except (httpx.HTTPError, ValueError, KeyError):
            return {}
        return {k: int(v) for k, v in counts.items() if isinstance(v, int)}

    def cost_row(self) -> dict[str, Any] | None:
        return {
            "variant": "models_only",
            "memory_usd_per_1000": 0.0,
            "basis": "no model call in its default configuration (local embeddings in process), "
            "servers not priced",
        }


class AiMemoryLlm(AiMemory):
    """The same server with its optional model consolidation at the end of each session
    (`AI_MEMORY_CONSOLIDATE_ON_SESSION_END=true`, `AI_MEMORY_LLM_PROVIDER=openai-compat`), through its
    model gateway, with the benchmark's extraction model."""

    system = "ai_memory_llm"
    title = "ai-memory (model consolidation)"
    url_env = "AI_MEMORY_LLM_URL"
    default_url = "http://ai-memory-llm:49374"
    meter_env = "AI_MEMORY_METER_URL"
    meter_default = "http://ai-memory-gateway:8081"
    consolidates = True

    def cost_row(self) -> dict[str, Any] | None:
        return None  # measured through its gateway
