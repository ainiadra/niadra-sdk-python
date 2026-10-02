"""The harness's door to Amazon Bedrock AgentCore Memory, a managed AWS service with no container to run.

AgentCore's API is HTTP with AWS Signature Version 4. Every other system is a server on the harness's
host that the adapters and the timed loops reach with plain HTTP, so this small service stands where that
server would: it takes the same paths AgentCore's data plane documents (`POST /memories/{id}/events`,
`/ingest`, `/retrieve`, `/memoryRecords`, `GET /memories/{id}/memoryRecord/{recordId}`), signs each request
with the host's own role and sends it to the regional endpoint over a kept connection. Requests and
answers go through untouched; nothing is logged or stored but the counters.

It also keeps the two things a managed service needs and a container gives for free:

- the resource: `POST /_bench/memory` creates an AgentCore Memory with the built-in strategies of
  config/agentcore.toml and answers once it is ACTIVE; `DELETE /_bench/memory/{id}` deletes it. Only
  memories this process created are deleted through it.
- the bill: `GET /_meter` counts the requests AWS charges for that were accepted (CreateEvent, and
  RetrieveMemoryRecords) and the others (IngestData, lists, gets), with the throttled and the failed ones
  apart. The run's cost line and its spend come from these counters and AWS's public prices.

- the budget: with `AGENTCORE_MAX_USD` set, a request that AWS would charge for is refused with 402 once
  the accepted ones add up to that much at AWS's public prices (lists counted at the retrieval price, the
  worst case), so a run cannot spend past its ceiling.

Settings: `AGENTCORE_REGION` (default `AWS_REGION`, then us-east-2); `AGENTCORE_UPSTREAM`, for a stand-in
of the service (`bench serve fake-agentcore`, local dry runs and tests), which is then called unsigned.
The credentials are the default chain's (the temporary host's instance role); no key is read or written.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import tomllib
import uuid
from typing import Any

import httpx

from niadra_bench import config as bench_config
from niadra_bench.services.asgi import (
    Receive,
    Scope,
    Send,
    lifespan,
    read_body,
    respond,
    respond_json,
)

SIGNING_NAME = "bedrock-agentcore"
DEFAULT_REGION = "us-east-2"
ACTIVE_TIMEOUT_S = 600.0
COUNTERS = ("events", "retrievals", "ingests", "lists", "gets", "other", "throttled", "failed", "refused")
BILLED = frozenset({"events", "retrievals", "ingests", "lists"})
#: The built-in strategies by the name config/agentcore.toml gives them: the CreateMemory member, the
#: strategy's name in the resource, and the namespace under the customer's root.
STRATEGIES: dict[str, tuple[str, str, str]] = {
    "semantic": ("semanticMemoryStrategy", "facts", "facts/"),
    "user_preference": ("userPreferenceMemoryStrategy", "preferences", "preferences/"),
    "summary": ("summaryMemoryStrategy", "summaries", "summaries/{sessionId}/"),
}


def strategy_inputs(names: list[str], root: str) -> list[dict[str, Any]]:
    """CreateMemory's `memoryStrategies` for the built-in strategies named, under one namespace root."""
    out: list[dict[str, Any]] = []
    for name in names:
        member, label, suffix = STRATEGIES[name]
        out.append({member: {"name": label, "namespaceTemplates": [f"{root}{suffix}"]}})
    return out


def _kind(method: str, path: str) -> str:
    """Which counter a data plane request belongs to."""
    if method == "POST" and path.endswith("/events"):
        return "events"
    if method == "POST" and path.endswith("/retrieve"):
        return "retrievals"
    if method == "POST" and path.endswith("/ingest"):
        return "ingests"
    if method == "POST" and path.endswith("/memoryRecords"):
        return "lists"
    if method == "GET" and "/memoryRecord/" in path:
        return "gets"
    return "other"


class AgentCoreProxy:
    def __init__(
        self,
        *,
        region: str | None = None,
        upstream: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_s: float = 5.0,
        max_usd: float | None = None,
    ) -> None:
        self.region = (
            region or os.environ.get("AGENTCORE_REGION") or os.environ.get("AWS_REGION") or DEFAULT_REGION
        )
        stand_in = upstream if upstream is not None else os.environ.get("AGENTCORE_UPSTREAM", "")
        # "aws" (the temporary host's setting) and empty both mean the real service.
        self.stand_in = stand_in.rstrip("/") if stand_in not in ("", "aws") else None
        self.data = self.stand_in or f"https://bedrock-agentcore.{self.region}.amazonaws.com"
        self.control = self.stand_in or f"https://bedrock-agentcore-control.{self.region}.amazonaws.com"
        self._client = httpx.AsyncClient(
            transport=transport, timeout=60.0, limits=httpx.Limits(max_connections=200)
        )
        self._credentials: Any = None
        self._poll_s = poll_s
        self.counters: dict[str, int] = dict.fromkeys(COUNTERS, 0)
        self.memories: dict[str, dict[str, Any]] = {}
        ceiling = os.environ.get("AGENTCORE_MAX_USD") or None
        self.max_usd = max_usd if max_usd is not None else (float(ceiling) if ceiling else None)
        with (bench_config.CONFIG_DIR / "agentcore.toml").open("rb") as handle:
            self.prices: dict[str, Any] = tomllib.load(handle)["prices"]

    @property
    def usd(self) -> float:
        """What the accepted requests cost at AWS's public prices; an IngestData counted as an event and
        a list as a retrieval (the price list names neither: the worst case). Storage is not in it."""
        count = self.counters
        writes, reads = count["events"] + count["ingests"], count["retrievals"] + count["lists"]
        return writes * float(self.prices["event"]) + reads * float(self.prices["retrieval"])

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": dict(self.counters),
            "usd": round(self.usd, 4),
            "max_usd": self.max_usd,
            "memories": sorted(self.memories),
            "region": self.region,
        }

    async def _signed(self, method: str, url: str, body: bytes) -> dict[str, str]:
        """The headers of the request, signed with the default credential chain (SigV4)."""
        headers = {"content-type": "application/json"}
        if self.stand_in:
            return headers
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.session import Session

        if self._credentials is None:
            # The first lookup may ask the instance metadata service: off the event loop.
            self._credentials = await asyncio.to_thread(Session().get_credentials)
            if self._credentials is None:
                raise RuntimeError("no AWS credentials: the proxy signs with the host's role")
        request = AWSRequest(method=method, url=url, data=body or None, headers=headers)
        SigV4Auth(self._credentials.get_frozen_credentials(), SIGNING_NAME, self.region).add_auth(request)
        return {str(k): str(v) for k, v in request.headers.items()}

    async def _call(self, base: str, method: str, path: str, payload: Any = None) -> httpx.Response:
        body = b"" if payload is None else json.dumps(payload).encode()
        url = f"{base}{path}"
        return await self._client.request(
            method, url, content=body or None, headers=await self._signed(method, url, body)
        )

    async def create_memory(self, spec: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """CreateMemory, then GetMemory until the resource and its strategies are ACTIVE."""
        request = {
            # The idempotency token the AWS SDKs fill in by themselves; the API refuses a call without it.
            "clientToken": uuid.uuid4().hex,
            "name": spec["name"],
            "description": "Temporary: Niadra's public benchmark (niadra-sdk-python, benchmarks/).",
            "eventExpiryDuration": int(spec.get("event_expiry_days", 365)),
            "memoryStrategies": strategy_inputs(list(spec["strategies"]), str(spec["namespace_root"])),
            "tags": {"niadra:bench": "agentcore-memory"},
        }
        created = await self._call(self.control, "POST", "/memories/create", request)
        if created.status_code // 100 != 2:
            return created.status_code, {"error": created.text[:2000]}
        memory = created.json()["memory"]
        self.memories[memory["id"]] = memory
        deadline = time.monotonic() + ACTIVE_TIMEOUT_S
        while memory.get("status") != "ACTIVE":
            if memory.get("status") == "FAILED" or time.monotonic() > deadline:
                return 502, {"error": f"memory {memory['id']} is {memory.get('status')}", "memory": memory}
            await asyncio.sleep(self._poll_s)
            details = await self._call(self.control, "GET", f"/memories/{memory['id']}/details")
            if details.status_code == 200:
                memory = details.json()["memory"]
        self.memories[memory["id"]] = memory
        strategies = {s["name"]: s["strategyId"] for s in memory.get("strategies", [])}
        return 200, {"memory_id": memory["id"], "strategies": strategies, "region": self.region}

    async def delete_memory(self, memory_id: str) -> tuple[int, dict[str, Any]]:
        if memory_id not in self.memories:
            return 404, {"error": "not a memory this proxy created"}
        # DeleteMemory takes its idempotency token in the query string, and refuses a call without one.
        deleted = await self._call(
            self.control, "DELETE", f"/memories/{memory_id}/delete?clientToken={uuid.uuid4().hex}"
        )
        if deleted.status_code // 100 != 2 and deleted.status_code != 404:
            return deleted.status_code, {"error": deleted.text[:2000]}
        self.memories.pop(memory_id, None)
        return 200, {"memory_id": memory_id, "status": "DELETING"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        path, method = scope["path"], scope["method"]
        if path == "/healthz":
            await respond_json(send, 200, {"status": "ok"})
            return
        if path == "/_meter":
            await respond_json(send, 200, self.snapshot())
            return
        body = await read_body(receive)
        if path == "/_bench/memory" and method == "POST":
            status, answer = await self.create_memory(json.loads(body or b"{}"))
            await respond_json(send, status, answer)
            return
        if path.startswith("/_bench/memory/") and method == "DELETE":
            status, answer = await self.delete_memory(path.rsplit("/", 1)[-1])
            await respond_json(send, status, answer)
            return
        if self.max_usd is not None and _kind(method, path) in BILLED and self.usd >= self.max_usd:
            self.counters["refused"] += 1
            await respond_json(
                send, 402, {"message": f"the benchmark's ceiling of US$ {self.max_usd} is reached"}
            )
            return
        query = scope.get("query_string", b"").decode()
        url = f"{self.data}{path}" + (f"?{query}" if query else "")
        upstream = await self._client.request(
            method, url, content=body or None, headers=await self._signed(method, url, body)
        )
        if upstream.status_code // 100 == 2:
            self.counters[_kind(method, path)] += 1
        elif upstream.status_code == 429:
            self.counters["throttled"] += 1
        else:
            self.counters["failed"] += 1
        content_type = upstream.headers.get("content-type", "application/json")
        await respond(
            send, upstream.status_code, upstream.content, [(b"content-type", content_type.encode())]
        )
