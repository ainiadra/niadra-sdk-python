"""The local cell's model answers on disk, and the spend they cost (`cell_server.py --real-models`).

`CachingTransport` sits under the httpx client that niadra-back's own OpenRouter adapters (Luna's
extraction and Jev's decisions) post through. Each request is keyed by a hash of the model, the prompt
version and the exact request body (its JSON with the keys sorted, so the key does not depend on the order
a dict was built in). A key already on disk answers from the file without a network call; any other
request goes to the provider, and a good answer (status 200, JSON with `choices` or `answers` and no
`error`) is written under its key. A failed call (a timeout, a status that is not 200, an error inside
the body) is never written, so the next run asks again.

The same inputs give the same key, so a rerun and both arms of an A/B reuse every answer whose request did
not change: an A/B that changes only the read path pays for its models once. Any change to what the model
reads (a prompt, a schema, an earlier answer in the same customer's history, the clock the events carry)
is a new key and a new call.

`Ledger` counts, per model, the calls, the answers from disk and from the provider, the failures, the
tokens, and the spend OpenRouter reported (`usage.cost`): `spend_usd` is what this process paid,
`saved_usd` what the answers read from disk cost when they were fetched. Nothing here logs a prompt, an
answer or a header.

Only httpx and the standard library, so the harness's tests import it without niadra-back.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

SCHEMA = "niadra-bench.model-cache.v1"
#: Headers that describe the bytes on the wire; the answer handed back is the decoded body.
_FRAMING = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def request_key(model: str, prompt_version: str, body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{SCHEMA}\n{model}\n{prompt_version}\n{canonical}".encode()).hexdigest()


def _cost(data: Any) -> float:
    usage = data.get("usage") if isinstance(data, dict) else None
    try:
        return float((usage or {}).get("cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _tokens(data: Any) -> tuple[int, int]:
    usage = (data.get("usage") if isinstance(data, dict) else None) or {}
    try:
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return 0, 0


def cacheable(status: int, data: Any) -> bool:
    """A complete answer: a chat completion with its choices, or a decision with its answers."""
    if status != 200 or not isinstance(data, dict) or "error" in data:
        return False
    return bool(data.get("choices")) or bool(data.get("answers"))


@dataclass
class ModelTally:
    calls: int = 0
    hits: int = 0
    misses: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    spend_usd: float = 0.0
    saved_usd: float = 0.0


class Ledger:
    def __init__(self) -> None:
        self.models: dict[str, ModelTally] = {}

    def tally(self, model: str) -> ModelTally:
        return self.models.setdefault(model, ModelTally())

    def snapshot(self) -> dict[str, Any]:
        models = {name: asdict(t) for name, t in sorted(self.models.items())}
        for tally in models.values():
            tally["spend_usd"] = round(tally["spend_usd"], 6)
            tally["saved_usd"] = round(tally["saved_usd"], 6)
        return {
            "models": models,
            "calls": sum(t.calls for t in self.models.values()),
            "hits": sum(t.hits for t in self.models.values()),
            "misses": sum(t.misses for t in self.models.values()),
            "failed": sum(t.failed for t in self.models.values()),
            "spend_usd": round(sum(t.spend_usd for t in self.models.values()), 6),
            "saved_usd": round(sum(t.saved_usd for t in self.models.values()), 6),
        }


class CachingTransport(httpx.AsyncBaseTransport):
    """`directory` None sends every request to the provider (`--no-model-cache`): nothing is read from disk
    and nothing is written. `misses` counts the requests sent to the provider, `failed` those of them that
    brought back no answer to keep."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        directory: Path | None,
        ledger: Ledger,
        prompt_versions: dict[str, str] | None = None,
        default_version: str = "",
    ) -> None:
        self._inner = inner
        self.directory = directory
        self.ledger = ledger
        self._versions = dict(prompt_versions or {})
        self._default_version = default_version
        self._locks: dict[str, asyncio.Lock] = {}

    def version(self, model: str) -> str:
        return self._versions.get(model, self._default_version)

    def path(self, key: str) -> Path:
        assert self.directory is not None
        return self.directory / key[:2] / f"{key}.json"

    @contextlib.asynccontextmanager
    async def _only_one(self, key: str) -> AsyncIterator[None]:
        """Two identical requests in flight at once: the second waits and reads what the first wrote."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    def _load(self, key: str) -> Any | None:
        try:
            entry = json.loads(self.path(key).read_text())
        except (OSError, ValueError):
            return None
        return entry.get("response") if entry.get("schema") == SCHEMA else None

    def _store(self, key: str, model: str, version: str, data: Any) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "schema": SCHEMA,
            "model": model,
            "prompt_version": version,
            "stored_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "response": data,
        }
        handle, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        with os.fdopen(handle, "w") as out:
            json.dump(entry, out, ensure_ascii=False)
        os.replace(tmp, target)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return await self._inner.handle_async_request(request)
        raw = await request.aread()
        try:
            body = json.loads(raw)
        except ValueError:
            return await self._inner.handle_async_request(request)
        model = str(body.get("model") or "") if isinstance(body, dict) else ""
        tally = self.ledger.tally(model or "unknown")
        tally.calls += 1
        version = self.version(model)
        key = request_key(model, version, body)
        async with self._only_one(key):
            if self.directory is not None and (cached := self._load(key)) is not None:
                tally.hits += 1
                tally.saved_usd += _cost(cached)
                return httpx.Response(200, json=cached, request=request)
            tally.misses += 1
            try:
                response = await self._inner.handle_async_request(request)
                content = await response.aread()
            except BaseException:
                tally.failed += 1
                raise
            try:
                data = json.loads(content)
            except ValueError:
                data = None
            tally.spend_usd += _cost(data)
            tokens_in, tokens_out = _tokens(data)
            tally.input_tokens += tokens_in
            tally.output_tokens += tokens_out
            if cacheable(response.status_code, data):
                if self.directory is not None:
                    self._store(key, model, version, data)
            else:
                tally.failed += 1
            headers = [(k, v) for k, v in response.headers.items() if k.lower() not in _FRAMING]
            return httpx.Response(response.status_code, headers=headers, content=content, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()
