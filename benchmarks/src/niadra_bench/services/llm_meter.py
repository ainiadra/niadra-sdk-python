"""A pass-through in front of OpenRouter that counts the tokens of every chat completion, per model.

Mem0's LLM calls go through it (`openrouter_base_url` in config/mem0.config.json), so the cost of
Mem0's extraction is measured from the provider's own usage numbers rather than estimated. The
harness reads `GET /_meter` before and after seeding. Requests and answers are forwarded untouched;
nothing is logged or stored but the counters.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from niadra_bench.services.asgi import (
    Receive,
    Scope,
    Send,
    lifespan,
    read_body,
    request_headers,
    respond,
    respond_json,
)

_HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}


class LlmMeter:
    def __init__(
        self, upstream: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.upstream = (upstream or os.environ.get("LLM_UPSTREAM", "https://openrouter.ai/api/v1")).rstrip(
            "/"
        )
        self._client = httpx.AsyncClient(transport=transport, timeout=180.0)
        self.models: dict[str, dict[str, int]] = {}

    def snapshot(self) -> dict[str, Any]:
        return {"models": {k: dict(v) for k, v in self.models.items()}}

    def _count(self, model: str, usage: dict[str, Any] | None, failed: bool) -> None:
        entry = self.models.setdefault(
            model, {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        entry["calls"] += 1
        if failed:
            entry["failed"] += 1
        if usage:
            entry["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            entry["completion_tokens"] += int(usage.get("completion_tokens") or 0)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        path = scope["path"]
        if path == "/_meter":
            if scope["method"] == "DELETE":
                self.models.clear()
            await respond_json(send, 200, self.snapshot())
            return
        if path == "/healthz":
            await respond_json(send, 200, {"status": "ok"})
            return
        body = await read_body(receive)
        headers = {k: v for k, v in request_headers(scope).items() if k not in _HOP}
        suffix = path.removeprefix("/v1") if path.startswith("/v1/") else path
        query = scope.get("query_string", b"").decode()
        url = f"{self.upstream}{suffix}" + (f"?{query}" if query else "")
        upstream = await self._client.request(scope["method"], url, content=body, headers=headers)
        if suffix.endswith("/chat/completions"):
            model = "unknown"
            try:
                model = str(json.loads(body or b"{}").get("model") or model)
                answer = upstream.json()
                self._count(model, answer.get("usage"), upstream.status_code != 200 or "error" in answer)
            except (json.JSONDecodeError, ValueError):
                self._count(model, None, True)
        out_headers = [
            (k.encode(), v.encode())
            for k, v in upstream.headers.items()
            if k.lower() not in _HOP and k.lower() != "content-encoding"
        ]
        await respond(send, upstream.status_code, upstream.content, out_headers)
