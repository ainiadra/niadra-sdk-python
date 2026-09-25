"""Stand-ins for the paid and heavy pieces, for local smoke runs and tests only. A run that used them
proves the plumbing works and nothing else; its numbers are never published.

- `FakeModels`: `POST /v1/embed` like niadra-models, with hashed bag-of-words vectors.
- `FakeLlm`: `POST /v1/chat/completions` like OpenRouter. For Mem0's extraction prompt it returns the
  new messages as memories, in the JSON shape Mem0 parses; for anything else, a short fixed answer.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

from niadra_bench.services.asgi import Receive, Scope, Send, lifespan, read_body, respond_json

_WORD = re.compile(r"\w+", re.UNICODE)


def hashed_vector(text: str, dim: int = 384) -> list[float]:
    vector = [0.0] * dim
    for word in _WORD.findall(text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vector[index] += 1.0 if digest[4] % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [round(v / norm, 6) for v in vector]


class FakeModels:
    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        if scope["path"] == "/healthz":
            await respond_json(send, 200, {"status": "ok"})
            return
        body = json.loads(await read_body(receive) or b"{}")
        texts = body.get("texts") or []
        await respond_json(
            send,
            200,
            {
                "vectors": [hashed_vector(t, self.dim) for t in texts],
                "dim": self.dim,
                "model_version": "fake-hash",
            },
        )


_NEW_MESSAGES = re.compile(r"(?:user|assistant)\s*:\s*(.+)", re.IGNORECASE)


class FakeLlm:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        body = json.loads(await read_body(receive) or b"{}")
        messages = body.get("messages") or []
        last = str(messages[-1].get("content", "")) if messages else ""
        wants_json = (body.get("response_format") or {}).get("type") == "json_object"
        if wants_json:
            lines = [m.group(1).strip() for m in _NEW_MESSAGES.finditer(last)][-4:] or [last[-400:]]
            content = json.dumps(
                {"memory": [{"id": str(i), "text": line[:400]} for i, line in enumerate(lines)]}
            )
        else:
            content = "5"
        prompt_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4
        await respond_json(
            send,
            200,
            {
                "id": "fake",
                "object": "chat.completion",
                "model": body.get("model", "fake"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(content) // 4},
            },
        )
