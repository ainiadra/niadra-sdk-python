"""An OpenAI-compatible `/v1/embeddings` in front of Niadra's embedding server (`niadra-models`,
`POST /v1/embed`), so Mem0's `openai` embedder uses the same model files Niadra uses.

Mem0 sends `dimensions` when `embedding_dims` is set; the proxy accepts it only when it equals the
model's size, since the model cannot produce another one. The OpenAI Python client asks for
`encoding_format: base64` by default, so both formats are served.

niadra-models takes at most 256 texts per request and 20,000 characters per text, and answers 413
otherwise; OpenAI's endpoint takes more. The proxy sends a longer list in pieces and cuts a longer text at
the limit, which changes no vector: the model reads the first 256 tokens of a text, far fewer characters
than the limit. (LangMem's store embedded more at once, and every write of it failed with the 413.)
"""

from __future__ import annotations

import base64
import json
import os
import struct

import httpx

from niadra_bench.services.asgi import Receive, Scope, Send, lifespan, read_body, respond_json

#: niadra-models' own limits per request (`NIADRA_MODELS_MAX_TEXTS`, `NIADRA_MODELS_MAX_CHARS` defaults).
MAX_TEXTS = 256
MAX_CHARS = 20_000


class EmbedProxy:
    def __init__(
        self,
        upstream: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        max_texts: int = MAX_TEXTS,
        max_chars: int = MAX_CHARS,
    ) -> None:
        self.upstream = (upstream or os.environ.get("NIADRA_MODELS_URL", "http://models:8080")).rstrip("/")
        self.max_texts = max_texts
        self.max_chars = max_chars
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
        vectors: list[list[float]] = []
        dim = 0
        for start in range(0, len(texts), self.max_texts):
            piece = [t[: self.max_chars] for t in texts[start : start + self.max_texts]]
            upstream = await self._client.post(f"{self.upstream}/v1/embed", json={"texts": piece})
            if upstream.status_code != 200:
                message = f"embedder answered {upstream.status_code}"
                await respond_json(send, 502, {"error": {"message": message}})
                return
            body = upstream.json()
            dim = int(body["dim"])
            vectors += body["vectors"]
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
        for index, vector in enumerate(vectors):
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
