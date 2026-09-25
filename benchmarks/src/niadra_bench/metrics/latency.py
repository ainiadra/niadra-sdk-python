"""Metric 1: how long the context takes to arrive before the model call, as the calling process sees it.

Open loop, like niadra-infra's scripts/latency.sh: requests leave at a constant rate for `duration_s`
whatever the answers do, spread over `conversations` seeded customers, after one warm-up call per
conversation. Niadra: `POST /v1/context` with the conversation id, the call an agent makes every turn.
Mem0: `POST /search` with the probe question, `top_k` and `threshold` from the config, the call its
documentation makes every turn. Both through the same HTTP client, from the same pod.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from niadra_bench.dataset.model import Case
from niadra_bench.identity import Identities
from niadra_bench.stats import distribution

Call = Callable[[httpx.AsyncClient, int], Awaitable[tuple[float, str]]]


@dataclass
class Probe:
    system: str
    path: str  # how the request travels: "cluster" (pod to service) or "edge" (public TLS address)
    call: Call
    # Only for dry runs against the in-process emulator.
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None


async def _timed(send: Awaitable[httpx.Response]) -> tuple[float, str]:
    started = time.perf_counter()
    try:
        response = await send
    except httpx.HTTPError as exc:
        return (time.perf_counter() - started) * 1000, type(exc).__name__
    elapsed = (time.perf_counter() - started) * 1000
    return elapsed, "ok" if response.status_code == 200 else f"http{response.status_code}"


def niadra_probe(
    path: str, base_url: str, key: str, pairs: Sequence[tuple[Case, Identities]], tag: str
) -> Probe:
    subjects = [
        ids.channel_handle(case.probe.channel).model_dump(mode="json", exclude_none=True)
        for case, ids in pairs
    ]
    conversations = [f"bench-{tag}-lat-{path}-{i}" for i in range(len(pairs))]
    headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
    url = f"{base_url.rstrip('/')}/v1/context"

    def call(client: httpx.AsyncClient, n: int) -> Awaitable[tuple[float, str]]:
        i = n % len(subjects)
        body = {"subject": subjects[i], "conversation_id": conversations[i]}
        return _timed(client.post(url, json=body, headers=headers))

    return Probe("niadra", path, call)


def mem0_probe(
    base_url: str, api_key: str, pairs: Sequence[tuple[Case, Identities]], top_k: int, threshold: float
) -> Probe:
    users = [ids.mem0_user("known_id", case.probe.channel) for case, ids in pairs]
    questions = [case.probe.question for case, _ in pairs]
    headers = {"x-api-key": api_key} if api_key else {}
    url = f"{base_url.rstrip('/')}/search"

    def call(client: httpx.AsyncClient, n: int) -> Awaitable[tuple[float, str]]:
        i = n % len(users)
        body = {
            "query": questions[i],
            "filters": {"user_id": users[i]},
            "top_k": top_k,
            "threshold": threshold,
        }
        return _timed(client.post(url, json=body, headers=headers))

    return Probe("mem0_oss", "cluster", call)


async def open_loop(
    probe: Probe, rate: int, duration_s: float, warmups: int, timeout_s: float
) -> list[tuple[float, str]]:
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
    transport = probe.transport() if probe.transport else None
    async with httpx.AsyncClient(timeout=timeout_s, limits=limits, transport=transport) as client:
        for n in range(warmups):
            await probe.call(client, n)
        results: list[tuple[float, str]] = []

        async def one(n: int) -> None:
            results.append(await probe.call(client, n))

        tasks: list[asyncio.Task[None]] = []
        start = time.perf_counter()
        for n in range(int(rate * duration_s)):
            delay = start + n / rate - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(n)))
        await asyncio.gather(*tasks)
        return results


async def run(
    probes: Sequence[Probe], rates: Sequence[int], duration_s: float, conversations: int, timeout_s: float
) -> list[dict[str, Any]]:
    """One line per system, path and rate. Systems run one after the other, never at the same time."""
    out: list[dict[str, Any]] = []
    for probe in probes:
        for rate in rates:
            results = await open_loop(probe, rate, duration_s, conversations, timeout_s)
            ok = [ms for ms, outcome in results if outcome == "ok"]
            errors: dict[str, int] = {}
            for _, outcome in results:
                if outcome != "ok":
                    errors[outcome] = errors.get(outcome, 0) + 1
            out.append(
                {
                    "system": probe.system,
                    "path": probe.path,
                    "rate": rate,
                    "duration_s": duration_s,
                    **distribution(ok),
                    "sent": len(results),
                    "errors": errors,
                }
            )
    return out
