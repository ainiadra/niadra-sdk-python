"""A pass-through in front of the model provider that counts the tokens of every model call, per model:
the model gateway of each measured system.

Every system's model calls go through one of these (Mem0's `openrouter_base_url` in
config/mem0.config.json; each other system's OpenAI-compatible base URL, in its compose file), so the
cost of its extraction is measured from the provider's own usage numbers rather than estimated. The
harness reads `GET /_meter` before seeding and after the memory settled. Requests and answers are
forwarded untouched; nothing is logged or stored but the counters.

Optional settings, for systems that take one OpenAI-compatible base URL for everything:

- `EMBED_UPSTREAM`: `/embeddings` goes there (the embedding proxy in front of the benchmark's embedder)
  instead of to the model provider, so a system with a single base URL embeds with the same model as
  every other system.
- `LLM_FORCE_MODEL`: every chat or responses call asks for this model. A system whose server fixes some
  of its model names (a "small model" for light prompts, say) then extracts with the same model as the
  others; the counters name the model that ran.
- `LLM_API_KEY`: the provider key, sent instead of whatever the system sent, so the system's own
  configuration never holds it.

Chat completions (`usage.prompt_tokens`, `usage.completion_tokens`) and the Responses API
(`usage.input_tokens`, `usage.output_tokens`) are both counted.
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
_MODEL_CALLS = ("/chat/completions", "/responses", "/completions")


class LlmMeter:
    def __init__(
        self,
        upstream: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        embed_upstream: str | None = None,
        force_model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.upstream = (upstream or os.environ.get("LLM_UPSTREAM", "https://openrouter.ai/api/v1")).rstrip(
            "/"
        )
        embed = embed_upstream if embed_upstream is not None else os.environ.get("EMBED_UPSTREAM", "")
        self.embed_upstream = embed.rstrip("/") or None
        self.force_model = (
            force_model if force_model is not None else os.environ.get("LLM_FORCE_MODEL") or None
        )
        self.api_key = api_key if api_key is not None else os.environ.get("LLM_API_KEY") or None
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
            entry["prompt_tokens"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            entry["completion_tokens"] += int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )

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
        model_call = suffix.endswith(_MODEL_CALLS)
        if suffix.endswith("/embeddings") and self.embed_upstream:
            base = self.embed_upstream
        else:
            base = self.upstream
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
        if model_call and self.force_model and body:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    payload["model"] = self.force_model
                    body = json.dumps(payload).encode()
            except json.JSONDecodeError:
                pass
        url = f"{base}{suffix}" + (f"?{query}" if query else "")
        upstream = await self._client.request(scope["method"], url, content=body, headers=headers)
        if model_call:
            model = "unknown"
            try:
                model = str(json.loads(body or b"{}").get("model") or model)
                answer = upstream.json()
                self._count(model, answer.get("usage"), upstream.status_code != 200 or "error" in answer)
            except (json.JSONDecodeError, ValueError, AttributeError):
                self._count(model, None, True)
        out_headers = [
            (k.encode(), v.encode())
            for k, v in upstream.headers.items()
            if k.lower() not in _HOP and k.lower() != "content-encoding"
        ]
        await respond(send, upstream.status_code, upstream.content, out_headers)
