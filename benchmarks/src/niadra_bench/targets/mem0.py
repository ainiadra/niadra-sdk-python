"""Mem0, used the way its documentation shows, in three forms:

- `Mem0RestTarget`: the open source REST server from Mem0's own repository (`server/`), in the same
  cluster, pgvector in its own database. `POST /memories` per exchange, `POST /search` per turn.
- `Mem0LibTarget`: `mem0.Memory` in-process with the same configuration plus a reranker, calling
  `search(..., rerank=True)`. It reads the store the REST server wrote (same collection), because the
  REST server's `/search` has no rerank parameter.
- `Mem0PlatformTarget`: the hosted Platform through `mem0ai`'s `MemoryClient`, with `MEM0_API_KEY`.
  Optional; its region is not published, so its latency includes the network to it.

What the agent receives is the search results in the format the Mem0 documentation uses:
"Based on previous conversations, I recall:" and one line per memory.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from niadra_bench import config as bench_config
from niadra_bench.dataset.model import Case, Session
from niadra_bench.identity import Identities, Scenario
from niadra_bench.targets.base import Retrieved, Stopwatch, Target

ADD_TIMEOUT_S = 180.0
SEARCH_TIMEOUT_S = 30.0


def render(header: str, results: list[dict[str, Any]]) -> str:
    memories = [str(item.get("memory") or "").strip() for item in results]
    lines = [f"- {m}" for m in memories if m]
    return f"{header}\n" + "\n".join(lines) if lines else ""


def exchanges(session: Session) -> list[list[dict[str, str]]]:
    """The session's turns as add() payloads: each customer message with the agent answer after it."""
    groups: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for turn in session.turns:
        role = "user" if turn.role == "customer" else "assistant"
        if role == "user" and current:
            groups.append(current)
            current = []
        current.append({"role": role, "content": turn.text})
    if current:
        groups.append(current)
    return groups


def add_payloads(case: Case, ids: Identities, scenario: Scenario, session: Session) -> list[dict[str, Any]]:
    """Every add() call for one session, in order."""
    user_id = ids.mem0_user(scenario, session.channel)
    metadata = {"channel": session.channel, "days_ago": session.days_ago}
    payloads: list[dict[str, Any]] = []
    if session.record is not None:
        text = ids.fill(session.record.text, case.customer.name)
        payloads.append(
            {
                "messages": [{"role": "user", "content": text}],
                "user_id": user_id,
                "infer": False,
                "metadata": {**metadata, "source": "system"},
            }
        )
    for group in exchanges(session):
        payloads.append({"messages": group, "user_id": user_id, "metadata": metadata})
    return payloads


class Mem0RestTarget(Target):
    system = "mem0_oss"

    def __init__(
        self,
        scenario: Scenario,
        settings: bench_config.Mem0Settings,
        *,
        url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = 8,
        configure: bool = True,
    ) -> None:
        self.scenario = scenario
        self.settings = settings
        self.url = (url or os.environ.get("MEM0_URL") or "http://localhost:8888").rstrip("/")
        self.api_key = api_key or os.environ.get("MEM0_SERVER_KEY", "")
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._limit = asyncio.Semaphore(concurrency)
        self._configure = configure
        self.counters = {"add_infer": 0, "add_raw": 0, "add_retries": 0, "search": 0}

    @property
    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key} if self.api_key else {}

    async def start(self) -> None:
        self._http = httpx.AsyncClient(transport=self._transport, base_url=self.url, headers=self.headers)
        if self._configure:
            # The server may still be starting (its migrations run first): wait for it, up to 3 minutes.
            for attempt in range(90):
                try:
                    response = await self._http.post(
                        "/configure", json=bench_config.mem0_config(), timeout=ADD_TIMEOUT_S
                    )
                    break
                except httpx.TransportError:
                    if attempt == 89:
                        raise
                    await asyncio.sleep(2)
            response.raise_for_status()

    async def _add(self, payload: dict[str, Any]) -> None:
        assert self._http is not None
        for attempt in range(4):
            response = await self._http.post("/memories", json=payload, timeout=ADD_TIMEOUT_S)
            if response.status_code < 500:
                response.raise_for_status()
                key = "add_raw" if payload.get("infer") is False else "add_infer"
                self.counters[key] += 1
                return
            self.counters["add_retries"] += 1
            await asyncio.sleep(2 * (attempt + 1))
        response.raise_for_status()

    async def seed(self, case: Case, ids: Identities) -> None:
        async with self._limit:
            for session in case.chronological():
                for payload in add_payloads(case, ids, self.scenario, session):  # type: ignore[arg-type]
                    await self._add(payload)

    async def search(self, user_id: str, query: str) -> tuple[list[dict[str, Any]], float]:
        assert self._http is not None
        body = {
            "query": query,
            "filters": {"user_id": user_id},
            "top_k": self.settings.top_k,
            "threshold": self.settings.threshold,
        }
        watch = Stopwatch()
        response = await self._http.post("/search", json=body, timeout=SEARCH_TIMEOUT_S)
        elapsed = watch.ms
        response.raise_for_status()
        self.counters["search"] += 1
        data = response.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        return list(results or []), elapsed

    async def retrieve(self, case: Case, ids: Identities, *, view: str | None = None) -> Retrieved:  # noqa: ARG002
        user_id = ids.mem0_user(self.scenario, case.probe.channel)  # type: ignore[arg-type]
        async with self._limit:
            try:
                results, elapsed = await self.search(user_id, case.probe.question)
            except httpx.HTTPError as exc:
                return Retrieved(text="", elapsed_ms=0.0, error=type(exc).__name__)
        return Retrieved(
            text=render(self.settings.render_header, results),
            elapsed_ms=elapsed,
            meta={"results": len(results)},
        )

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def versions(self) -> dict[str, str]:
        return {"mem0_server": os.environ.get("MEM0_SERVER_VERSION", "unknown")}


class Mem0LibTarget(Target):
    """`mem0.Memory` in-process, reading the REST server's store, with `rerank=True`."""

    system = "mem0_oss_rerank"
    seeds = False

    def __init__(self, scenario: Scenario, settings: bench_config.Mem0Settings, concurrency: int = 4) -> None:
        self.scenario = scenario
        self.settings = settings
        self._memory: Any = None
        self._limit = asyncio.Semaphore(concurrency)

    async def start(self) -> None:
        from mem0 import Memory

        config = bench_config.mem0_config(reranker=True)
        store = config["vector_store"]["config"]
        store.update(
            {
                "host": os.environ["POSTGRES_HOST"],
                "port": int(os.environ.get("POSTGRES_PORT", "5432")),
                "dbname": os.environ["POSTGRES_DB"],
                "user": os.environ["POSTGRES_USER"],
                "password": os.environ["POSTGRES_PASSWORD"],
            }
        )
        self._memory = await asyncio.to_thread(Memory.from_config, config)

    async def seed(self, case: Case, ids: Identities) -> None:  # noqa: ARG002
        raise RuntimeError("the rerank target reads what the REST target seeded")

    async def retrieve(self, case: Case, ids: Identities, *, view: str | None = None) -> Retrieved:  # noqa: ARG002
        user_id = ids.mem0_user(self.scenario, case.probe.channel)  # type: ignore[arg-type]
        async with self._limit:
            watch = Stopwatch()
            try:
                data = await asyncio.to_thread(
                    self._memory.search,
                    case.probe.question,
                    filters={"user_id": user_id},
                    top_k=self.settings.top_k,
                    threshold=self.settings.threshold,
                    rerank=True,
                )
            except Exception as exc:
                return Retrieved(text="", elapsed_ms=watch.ms, error=type(exc).__name__)
            elapsed = watch.ms
        results = data.get("results", []) if isinstance(data, dict) else list(data)
        return Retrieved(
            text=render(self.settings.render_header, results),
            elapsed_ms=elapsed,
            meta={"results": len(results)},
        )

    def versions(self) -> dict[str, str]:
        from importlib.metadata import version

        return {"mem0ai": version("mem0ai")}


class Mem0PlatformTarget(Target):
    """The hosted Mem0 Platform (optional). Its add() is processed in the background (PENDING)."""

    system = "mem0_platform"

    def __init__(self, scenario: Scenario, settings: bench_config.Mem0Settings, concurrency: int = 4) -> None:
        self.scenario = scenario
        self.settings = settings
        self._client: Any = None
        self._limit = asyncio.Semaphore(concurrency)

    async def start(self) -> None:
        from mem0 import MemoryClient

        self._client = MemoryClient(api_key=os.environ["MEM0_API_KEY"])

    async def seed(self, case: Case, ids: Identities) -> None:
        async with self._limit:
            for session in case.chronological():
                for payload in add_payloads(case, ids, self.scenario, session):  # type: ignore[arg-type]
                    messages = payload.pop("messages")
                    await asyncio.to_thread(self._client.add, messages, **payload)

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:  # noqa: ARG002
        # The Platform extracts in the background and publishes no completion signal per user.
        await asyncio.sleep(120)
        return {"settled": True, "seconds": 120, "method": "fixed wait"}

    async def retrieve(self, case: Case, ids: Identities, *, view: str | None = None) -> Retrieved:  # noqa: ARG002
        user_id = ids.mem0_user(self.scenario, case.probe.channel)  # type: ignore[arg-type]
        async with self._limit:
            watch = Stopwatch()
            try:
                data = await asyncio.to_thread(
                    self._client.search,
                    case.probe.question,
                    filters={"user_id": user_id},
                    top_k=self.settings.top_k,
                    threshold=self.settings.threshold,
                )
            except Exception as exc:
                return Retrieved(text="", elapsed_ms=watch.ms, error=type(exc).__name__)
            elapsed = watch.ms
        results = data.get("results", []) if isinstance(data, dict) else list(data)
        return Retrieved(text=render(self.settings.render_header, results), elapsed_ms=elapsed)
