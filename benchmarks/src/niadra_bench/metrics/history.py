"""Metric 8: how long a history navigation call takes (the docs promise `search` and `open` in under
200 ms, in the region), measured like metric 1 (`operations`).

Niadra, as a voice agent in the middle of a call that proved the caller at V1 (verified before the
clock starts, as every accuracy probe and the freshness reader do):

- `search`: `POST /v1/history/search`, the call behind `AsyncNiadra.search()` and the
  `search_customer_history` tool, with the case's probe question, the customer's phone, the SDK's
  default `max_tokens` and the conversation id;
- `open`: `POST /v1/history/open`, the call behind `open()` and the `open_history_item` tool, on an
  episode or object that a search (or, failing that, the timeline) of the same customer returned
  before the clock started.

The bodies are the SDK's own request models, sent through a plain HTTP client, so the time is the
server's answer and not the SDK's navigation budget (the SDK gives up at 0.6 s, 0.3 s on voice, and
returns an empty result). Both from inside the cluster (`NIADRA_CLUSTER_URL`, the `read` service) and
through the public TLS address.

Mem0 has one read, `search`, used for both the turn's memory and any lookup: the closest equivalent of
a history search is the same `POST /search` metric 1 times, with the same probe question, `top_k` and
`threshold`, for the same seeded user (`known_id`). Mem0 has no episode or conversation to open; the
nearest call to `open` is `GET /memories/{memory_id}`, reading one memory a search returned. From
inside the cluster only: Mem0's server has no public address here.

Both searches start by encoding the question with the same embedding server (`niadra-models`: Niadra's
read service calls it, Mem0 through the embedding proxy). So that a search's time can be read without
it, a third line times that step alone: `encode`, `POST /v1/embed` with the same probe questions at the
same rates, from inside the cluster (`NIADRA_MODELS_URL`; no line without it). Niadra's search line
also keeps the steps its server names in `Server-Timing` (`server_timing`), so an encoding step the
server reports shows there too.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from typing import Any, Final

import httpx
from niadra.models import OpenItemRequest, SearchRequest, TimelineRequest

from niadra_bench.config import HistorySettings, Mem0Settings
from niadra_bench.dataset.model import Case
from niadra_bench.identity import Identities
from niadra_bench.metrics import latency
from niadra_bench.metrics.operations import (
    DEGRADED,
    EMPTY,
    OK,
    Operation,
    measure,
    paths,
    recording,
    timed,
)
from niadra_bench.targets.mem0 import Mem0RestTarget
from niadra_bench.targets.niadra import NiadraTarget

log = logging.getLogger("niadra_bench")

#: The items `open` accepts (the server's `ItemKind.EPISODE` and `ItemKind.OBJECT`).
OPENABLE = ("episode", "object")
#: The level the voice agent proves before any read, and how.
LEVEL: Final = "V1"
METHOD: Final = "network_attestation"


def _body(model: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = model.model_dump(mode="json", exclude_none=True)
    return dumped


def niadra_search_outcome(response: httpx.Response) -> str:
    data = response.json()
    if data.get("degraded"):
        return DEGRADED
    return OK if data.get("items") else EMPTY


def mem0_search_outcome(response: httpx.Response) -> str:
    data = response.json()
    results = data.get("results", data) if isinstance(data, dict) else data
    return OK if results else EMPTY


def _openable(items: Sequence[dict[str, Any]]) -> list[str]:
    return [str(i["id"]) for i in items if i.get("kind") in OPENABLE and i.get("id")]


class NiadraConversation:
    """One seeded customer as the voice agent reads it: the handle, a verified conversation per path,
    the probe question and an item to open."""

    def __init__(self, case: Case, ids: Identities, conversation: str) -> None:
        self.subject = ids.channel_handle("voice")
        self.question = case.probe.question
        self.conversation = conversation
        self.item: str | None = None


async def niadra_operations(
    target: NiadraTarget,
    pairs: Sequence[tuple[Case, Identities]],
    tag: str,
    settings: HistorySettings,
    where: dict[str, str],
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> list[Operation]:
    """Proves every conversation at V1 and finds an item to open, then builds the timed calls."""
    voice = target.client("voice")
    _, key = target.keys.for_channel("voice")
    headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
    ops_search: list[Operation] = []
    ops_open: list[Operation] = []
    async with httpx.AsyncClient(
        transport=transport() if transport else None, timeout=settings.request_timeout_s
    ) as http:
        for path, address in where.items():
            base = address.rstrip("/")
            slots = [
                NiadraConversation(c, i, f"bench-{tag}-his-{path}-{n}") for n, (c, i) in enumerate(pairs)
            ]
            for slot in slots:
                await voice.verify(METHOD, LEVEL, handle=slot.subject, conversation_id=slot.conversation)
                slot.item = await _find_item(http, base, headers, slot, settings.max_tokens)
            ops_search.append(_niadra_search(path, base, headers, slots, settings.max_tokens, transport))
            ops_open.append(_niadra_open(path, base, headers, slots, transport))
    return ops_search + ops_open


async def _find_item(
    http: httpx.AsyncClient, base: str, headers: dict[str, str], slot: NiadraConversation, max_tokens: int
) -> str | None:
    """An episode or object of this customer that opens at the conversation's level: from the search
    the agent would make, else from the timeline. Found before the clock starts."""
    search = SearchRequest(
        subject=slot.subject,
        query=slot.question,
        max_tokens=max_tokens,
        verification=LEVEL,
        conversation_id=slot.conversation,
    )
    timeline = TimelineRequest(subject=slot.subject, verification=LEVEL, conversation_id=slot.conversation)
    for route, request in (("search", search), ("timeline", timeline)):
        response = await http.post(f"{base}/v1/history/{route}", json=_body(request), headers=headers)
        if response.status_code != 200:
            continue
        for item in _openable(response.json().get("items") or []):
            opened = await http.post(f"{base}/v1/history/open", json=_open_body(slot, item), headers=headers)
            if opened.status_code == 200:
                return item
    return None


def _open_body(slot: NiadraConversation, item: str) -> dict[str, Any]:
    request = OpenItemRequest(
        item_id=item, subject=slot.subject, verification=LEVEL, conversation_id=slot.conversation
    )
    return _body(request)


def _niadra_search(
    path: str,
    base: str,
    headers: dict[str, str],
    slots: Sequence[NiadraConversation],
    max_tokens: int,
    transport: Callable[[], httpx.AsyncBaseTransport] | None,
) -> Operation:
    bodies = [
        _body(
            SearchRequest(
                subject=s.subject,
                query=s.question,
                max_tokens=max_tokens,
                verification=LEVEL,
                conversation_id=s.conversation,
            )
        )
        for s in slots
    ]
    url = f"{base}/v1/history/search"
    timings: dict[str, list[float]] = {}
    classify = recording(niadra_search_outcome, timings)

    async def call(client: httpx.AsyncClient, n: int) -> tuple[float, str]:
        body = bodies[n % len(bodies)]
        return await timed(client.post(url, json=body, headers=headers), 200, classify)

    probe = latency.Probe("niadra", path, call, transport)
    return Operation(probe, "search", "POST /v1/history/search", timings=timings)


def encode_operation(
    models_url: str,
    pairs: Sequence[tuple[Case, Identities]],
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> Operation:
    """The question's encoding alone, on the embedding server both searches call first."""
    url = f"{models_url.rstrip('/')}/v1/embed"
    bodies = [{"texts": [case.probe.question]} for case, _ in pairs]

    async def call(client: httpx.AsyncClient, n: int) -> tuple[float, str]:
        return await timed(client.post(url, json=bodies[n % len(bodies)]), 200)

    return Operation(latency.Probe("embedder", "cluster", call, transport), "encode", "POST /v1/embed")


def _niadra_open(
    path: str,
    base: str,
    headers: dict[str, str],
    slots: Sequence[NiadraConversation],
    transport: Callable[[], httpx.AsyncBaseTransport] | None,
) -> Operation:
    bodies = [_open_body(s, s.item) for s in slots if s.item]
    url = f"{base}/v1/history/open"

    async def call(client: httpx.AsyncClient, n: int) -> tuple[float, str]:
        return await timed(client.post(url, json=bodies[n % len(bodies)], headers=headers), 200)

    skipped = None if bodies else "no episode or object to open in any conversation"
    return Operation(latency.Probe("niadra", path, call, transport), "open", "POST /v1/history/open", skipped)


async def mem0_operations(
    target: Mem0RestTarget,
    pairs: Sequence[tuple[Case, Identities]],
    settings: Mem0Settings,
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> list[Operation]:
    """Mem0's search (the same call as metric 1) and its one-memory read, on ids a search returned."""
    users = [ids.mem0_user("known_id", case.probe.channel) for case, ids in pairs]
    questions = [case.probe.question for case, _ in pairs]
    base = target.url.rstrip("/")
    headers = target.headers
    bodies = [
        {"query": q, "filters": {"user_id": u}, "top_k": settings.top_k, "threshold": settings.threshold}
        for u, q in zip(users, questions, strict=True)
    ]
    memory_ids: list[str] = []
    async with httpx.AsyncClient(
        transport=transport() if transport else None, headers=headers, timeout=30
    ) as http:
        for body in bodies:
            response = await http.post(f"{base}/search", json=body)
            if response.status_code != 200:
                continue
            data = response.json()
            results = data.get("results", data) if isinstance(data, dict) else data
            if first := next((str(r["id"]) for r in results or [] if r.get("id")), None):
                memory_ids.append(first)

    async def search(client: httpx.AsyncClient, n: int) -> tuple[float, str]:
        send = client.post(f"{base}/search", json=bodies[n % len(bodies)], headers=headers)
        return await timed(send, 200, mem0_search_outcome)

    async def get(client: httpx.AsyncClient, n: int) -> tuple[float, str]:
        memory = memory_ids[n % len(memory_ids)]
        return await timed(client.get(f"{base}/memories/{memory}", headers=headers), 200)

    return [
        Operation(latency.Probe("mem0_oss", "cluster", search, transport), "search", "POST /search"),
        Operation(
            latency.Probe("mem0_oss", "cluster", get, transport),
            "open",
            "GET /memories/{memory_id}",
            None if memory_ids else "no memory returned by any search",
        ),
    ]


async def run(
    niadra: NiadraTarget | None,
    mem0: Mem0RestTarget | None,
    pairs: Sequence[tuple[Case, Identities]],
    tag: str,
    settings: HistorySettings,
    mem0_settings: Mem0Settings,
    *,
    rates: Sequence[int],
    duration_s: float,
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
    mem0_transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
    models_url: str | None = None,
    models_transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> list[dict[str, Any]]:
    ops: list[Operation] = []
    models_url = models_url or os.environ.get("NIADRA_MODELS_URL")
    if models_url and (niadra is not None or mem0 is not None):
        ops.append(encode_operation(models_url, pairs, models_transport))
    if niadra is not None:
        where = paths(niadra.client("voice").base_url, "NIADRA_CLUSTER_URL")
        ops += await niadra_operations(niadra, pairs, tag, settings, where, transport)
    if mem0 is not None:
        ops += await mem0_operations(mem0, pairs, mem0_settings, mem0_transport)
    for op in ops:
        if op.skipped:
            log.warning(
                "history: %s %s %s not measured: %s", op.probe.system, op.probe.path, op.operation, op.skipped
            )
    return await measure(ops, rates, duration_s, len(pairs), settings.request_timeout_s)
