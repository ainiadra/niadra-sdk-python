"""Metric 7: what the agent's turn sees when the memory is slow or down.

The same fault proxy stands in front of each memory service: once holding every request for
`delay_ms`, once answering `status` (503). Each trial is the read a turn makes before the model call,
with the client as it ships:

- Niadra: `AsyncNiadra.context(view="voice")` with the SDK's default time budgets. It never raises;
  past its budget it returns an empty context.
- Mem0: `POST /search` on its REST server with an HTTP client at its defaults (httpx, 5 s) and
  `raise_for_status()`, the way its REST examples call it. Mem0's REST server has no official
  Python client, so there is no budget or fallback to inherit.
- The systems added through `niadra_bench.systems`: their read, called the same way as Mem0's
  (`HttpSystem.resilience_trials`).

For each trial: the time until the agent can call its model, whether that fits the voice turn budget,
whether an exception reached the agent's code, and whether the memory block came back empty.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Protocol

import httpx
from niadra import AsyncNiadra, CacheOptions

from niadra_bench.dataset.model import Case
from niadra_bench.identity import Identities
from niadra_bench.services.fault_proxy import Fault, FaultProxy
from niadra_bench.services.serve import background
from niadra_bench.stats import distribution, rate


class Faulty(Protocol):
    system: str
    url: str
    #: How to reach the server (None: the network); in-process fakes in tests.
    transport: httpx.AsyncBaseTransport | None

    async def resilience_trials(
        self, proxy_url: str, pairs: Sequence[tuple[Case, Identities]]
    ) -> list[tuple[float, bool, bool]]: ...


async def _niadra_trials(
    proxy_url: str, key: str, pairs: Sequence[tuple[Case, Identities]], tag: str
) -> list[tuple[float, bool, bool]]:
    out: list[tuple[float, bool, bool]] = []
    async with AsyncNiadra(key, base_url=proxy_url, cache=CacheOptions(enabled=False)) as client:
        for n, (_case, ids) in enumerate(pairs):
            started = time.perf_counter()
            raised = False
            empty = True
            try:
                context = await client.context(
                    ids.channel_handle("voice"),
                    view="voice",
                    verification="V1",
                    conversation_id=f"bench-{tag}-res-{n}",
                )
                empty = not context.system_block and not context.turn_block
            except Exception:
                raised = True
            out.append(((time.perf_counter() - started) * 1000, raised, empty))
    return out


async def _mem0_trials(
    proxy_url: str, api_key: str, pairs: Sequence[tuple[Case, Identities]], top_k: int, threshold: float
) -> list[tuple[float, bool, bool]]:
    out: list[tuple[float, bool, bool]] = []
    headers = {"x-api-key": api_key} if api_key else {}
    async with httpx.AsyncClient(headers=headers) as client:
        for case, ids in pairs:
            started = time.perf_counter()
            raised = False
            empty = True
            try:
                response = await client.post(
                    f"{proxy_url}/search",
                    json={
                        "query": case.probe.question,
                        "filters": {"user_id": ids.mem0_user("known_id", "voice")},
                        "top_k": top_k,
                        "threshold": threshold,
                    },
                )
                response.raise_for_status()
                empty = not response.json().get("results")
            except Exception:
                raised = True
            out.append(((time.perf_counter() - started) * 1000, raised, empty))
    return out


def _summary(
    system: str, fault: Fault, trials: list[tuple[float, bool, bool]], budget_ms: int
) -> dict[str, Any]:
    times = [t for t, _, _ in trials]
    return {
        "system": system,
        "fault": fault.name,
        "trials": len(trials),
        "memory_stage_ms": distribution(times),
        "within_budget_rate": rate(sum(1 for t in times if t <= budget_ms), len(times)),
        "raised_rate": rate(sum(1 for _, r, _ in trials if r), len(trials)),
        "empty_rate": rate(sum(1 for _, _, e in trials if e), len(trials)),
        "turn_budget_ms": budget_ms,
    }


async def run(
    *,
    niadra_upstream: str | None,
    niadra_key: str | None,
    mem0_upstream: str | None,
    mem0_key: str,
    pairs: Sequence[tuple[Case, Identities]],
    tag: str,
    delay_ms: int,
    status: int,
    budget_ms: int,
    top_k: int,
    threshold: float,
    transport: httpx.AsyncBaseTransport | None = None,
    others: Sequence[Faulty] = (),
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fault in (Fault(delay_ms=delay_ms), Fault(status=status)):
        if niadra_upstream and niadra_key:
            async with background(FaultProxy(niadra_upstream, fault, transport=transport)) as url:
                trials = await _niadra_trials(url, niadra_key, pairs, f"{tag}{fault.name}")
            out.append(_summary("niadra", fault, trials, budget_ms))
        if mem0_upstream:
            async with background(FaultProxy(mem0_upstream, fault)) as url:
                trials = await _mem0_trials(url, mem0_key, pairs, top_k, threshold)
            out.append(_summary("mem0_oss", fault, trials, budget_ms))
        for other in others:
            async with background(FaultProxy(other.url, fault, transport=other.transport)) as url:
                trials = await other.resilience_trials(url, pairs)
            out.append(_summary(other.system, fault, trials, budget_ms))
    return out
