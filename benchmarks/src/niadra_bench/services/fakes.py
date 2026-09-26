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
from typing import Any

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
    """niadra-models' `/v1/embed`, with its limits per request (413 past them)."""

    def __init__(self, dim: int = 384, max_texts: int = 256, max_chars: int = 20_000) -> None:
        self.dim = dim
        self.max_texts = max_texts
        self.max_chars = max_chars
        self.requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        if scope["path"] == "/healthz":
            await respond_json(send, 200, {"status": "ok"})
            return
        body = json.loads(await read_body(receive) or b"{}")
        texts = body.get("texts") or []
        self.requests += 1
        if len(texts) > self.max_texts or any(len(t) > self.max_chars for t in texts):
            await respond_json(send, 413, {"detail": "too many texts or characters"})
            return
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


def minimal(schema: Any, defs: dict[str, Any] | None = None, depth: int = 0) -> Any:
    """The smallest value a JSON schema accepts: required fields only, empty lists, empty strings,
    zeros, the first enum value. Lets a system that asks for structured output parse the answer."""
    if not isinstance(schema, dict) or depth > 12:
        return None
    defs = {**(defs or {}), **schema.get("$defs", {}), **schema.get("definitions", {})}
    if ref := schema.get("$ref"):
        return minimal(defs.get(str(ref).rsplit("/", 1)[-1], {}), defs, depth + 1)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("anyOf", "oneOf", "allOf"):
        if options := schema.get(key):
            typed = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
            return minimal((typed or options)[0], defs, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {name: minimal(props.get(name, {}), defs, depth + 1) for name in schema.get("required", [])}
    if kind == "array":
        return []
    if kind == "string":
        return ""
    if kind in ("number", "integer"):
        return 0
    if kind == "boolean":
        return False
    return None


def _schema(body: dict[str, Any]) -> dict[str, Any] | None:
    """The JSON schema a chat or responses call asks its answer to follow, if any."""
    fmt = body.get("response_format") or (body.get("text") or {}).get("format") or {}
    if fmt.get("type") != "json_schema":
        return None
    schema = fmt.get("json_schema", fmt)
    found = schema.get("schema") if isinstance(schema, dict) else None
    return found if isinstance(found, dict) else None


class FakeLlm:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        body = json.loads(await read_body(receive) or b"{}")
        if scope["path"].endswith("/responses"):
            await self._responses(send, body)
            return
        messages = body.get("messages") or []
        last = str(messages[-1].get("content", "")) if messages else ""
        schema = _schema(body)
        wants_json = (body.get("response_format") or {}).get("type") == "json_object"
        if body.get("tools"):
            await self._tool_call(send, body)
            return
        if schema is not None:
            content = json.dumps(minimal(schema))
        elif wants_json:
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
                "created": 0,
                "model": body.get("model", "fake"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": len(content) // 4,
                    "total_tokens": prompt_tokens + len(content) // 4,
                },
            },
        )

    async def _tool_call(self, send: Send, body: dict[str, Any]) -> None:
        """A model that must call a tool (an agent loop): it calls the one that ends the loop when there
        is one (`done`, `final_answer`...), else the first, with the smallest arguments it accepts."""
        tools = [t.get("function", t) for t in body["tools"] if isinstance(t, dict)]
        ending = ("done", "final_answer", "answer", "finish", "respond", "submit")
        chosen = next((t for t in tools if str(t.get("name", "")).lower() in ending), tools[0])
        arguments = json.dumps(minimal(chosen.get("parameters") or {"type": "object"}) or {})
        await respond_json(
            send,
            200,
            {
                "id": "fake",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "fake"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_fake",
                                    "type": "function",
                                    "function": {"name": chosen.get("name", "tool"), "arguments": arguments},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async def _responses(self, send: Send, body: dict[str, Any]) -> None:
        """The Responses API, as far as structured output goes."""
        schema = _schema(body)
        text = json.dumps(minimal(schema)) if schema is not None else "5"
        prompt = len(json.dumps(body.get("input", ""))) // 4
        await respond_json(
            send,
            200,
            {
                "id": "resp_fake",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "error": None,
                "model": body.get("model", "fake"),
                "output": [
                    {
                        "id": "msg_fake",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
                "usage": {
                    "input_tokens": prompt,
                    "output_tokens": len(text) // 4,
                    "total_tokens": prompt + len(text) // 4,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )
