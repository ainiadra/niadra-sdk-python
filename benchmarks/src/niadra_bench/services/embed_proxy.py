"""An OpenAI-compatible `/v1/embeddings` in front of Niadra's embedding server (`niadra-models`,
`POST /v1/embed`), so Mem0's `openai` embedder uses the same model files Niadra uses.

Mem0 sends `dimensions` when `embedding_dims` is set; the proxy accepts it only when it equals the
model's size, since the model cannot produce another one. The OpenAI Python client asks for
`encoding_format: base64` by default, so both formats are served.
"""

from __future__ import annotations

import base64
import json
import os
import struct

import httpx

from niadra_bench.services.asgi import Receive, Scope, Send, lifespan, read_body, respond_json


class EmbedProxy:
    def __init__(
        self, upstream: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.upstream = (upstream or os.environ.get("NIADRA_MODELS_URL", "http://models:8080")).rstrip("/")
        self._client = httpx.AsyncClient(transport=transport, timeout=60.0)
        self.calls = 0
        self.texts = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        path = scope["path"]
        if path in ("/healthz", "/v1/healthz"):
            await respond_json(send, 200, {"status": "ok", "calls": self.calls, "texts": self.texts})
            return
        if scope["method"] != "POST" or path not in ("/v1/embeddings", "/embeddings"):
            await respond_json(send, 404, {"error": {"message": "not found"}})
            return
        try:
            request = json.loads(await read_body(receive) or b"{}")
        except json.JSONDecodeError:
            await respond_json(send, 400, {"error": {"message": "invalid JSON"}})
            return
        raw = request.get("input")
        texts = [raw] if isinstance(raw, str) else list(raw or [])
        if not texts or not all(isinstance(t, str) for t in texts):
            await respond_json(
                send, 400, {"error": {"message": "input must be a string or a list of strings"}}
            )
            return
        upstream = await self._client.post(f"{self.upstream}/v1/embed", json={"texts": texts})
        if upstream.status_code != 200:
            await respond_json(send, 502, {"error": {"message": f"embedder answered {upstream.status_code}"}})
            return
        body = upstream.json()
        dim = int(body["dim"])
        wanted = request.get("dimensions")
        if wanted is not None and int(wanted) != dim:
            await respond_json(
                send, 400, {"error": {"message": f"this model has {dim} dimensions, not {wanted}"}}
            )
            return
        self.calls += 1
        self.texts += len(texts)
        as_base64 = request.get("encoding_format") == "base64"
        data = []
        for index, vector in enumerate(body["vectors"]):
            if as_base64:
                embedding: object = base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode()
            else:
                embedding = vector
            data.append({"object": "embedding", "index": index, "embedding": embedding})
        await respond_json(
            send,
            200,
            {
                "object": "list",
                "data": data,
                "model": request.get("model") or body.get("model_version", "niadra-models"),
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )
