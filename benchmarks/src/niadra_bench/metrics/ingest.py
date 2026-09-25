"""Metric 9: how long the memory takes to acknowledge a write (the docs promise the ingestion
acknowledgement in under 80 ms, in the region), measured like metric 1 (`operations`).

Every call carries one exchange, the customer's message and the agent's answer, for one of the
seeded customers, `turns_per_conversation` exchanges per conversation, each with a new number so no
write repeats another.

- Niadra: `POST /v1/batch` with the two message items, the request `track()` makes when a turn leaves
  the SDK's queue (the SDK's own `EventItem` models), timed until the `200` that acknowledges them.
  `track()` itself never waits: it queues and returns; this is the acknowledgement the queue waits
  for. Niadra writes the events durably before it answers and extracts memory later, in the
  background. From inside the cluster (`NIADRA_CLUSTER_INGEST_URL`, the `ingest` service) and
  through the public TLS address.
- Mem0: `POST /memories` on its REST server with the same two messages, the call its README makes
  per exchange, timed until its `200`. The server has no asynchronous mode (the hosted Platform's
  `async_mode` is not in the open source server), so it is measured both ways it offers: `add_infer`
  (its default, `infer` true: the extraction model runs before the answer, as its documentation
  recommends for conversations) and `add_raw` (`infer=False`: the text is embedded and stored as is,
  with no extraction ever, the nearest to an acknowledgement that defers the work). From inside the
  cluster only.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from niadra import Content, EventItem, Speaker, SpeakerRef

from niadra_bench.config import IngestSettings
from niadra_bench.dataset.model import Case
from niadra_bench.identity import Identities
from niadra_bench.metrics import latency
from niadra_bench.metrics.operations import Operation, measure, paths, timed
from niadra_bench.targets.mem0 import Mem0RestTarget
from niadra_bench.targets.niadra import NiadraTarget

log = logging.getLogger("niadra_bench")

MESSAGES = {
    "pt": (
        "Oi, o número do meu pedido novo é {code}, pode anotar?",
        "Anotado: o pedido {code} está no seu cadastro.",
    ),
    "en": (
        "Hi, my new order number is {code}, can you note it?",
        "Noted: order {code} is on your account.",
    ),
}


def exchange(language: str, seq: int) -> tuple[str, str]:
    code = f"{700000 + seq % 300000}"
    customer, agent = MESSAGES.get(language, MESSAGES["en"])
    return customer.format(code=code), agent.format(code=code)


def niadra_items(
    case: Case, ids: Identities, conversation: str, seq: int, key_prefix: str
) -> list[dict[str, Any]]:
    """The two message items of one exchange, as `track()` would send them."""
    customer, agent = exchange(case.language, seq)
    handle = ids.channel_handle("whatsapp")
    items = []
    for suffix, text, speaker, direction in (
        ("c", customer, Speaker.CUSTOMER, "inbound"),
        ("a", agent, Speaker.AI_AGENT, "outbound"),
    ):
        event = EventItem(
            kind="message",
            idempotency_key=f"{key_prefix}-{seq}-{suffix}",
            channel="whatsapp",
            conversation_id=conversation,
            handles=[handle],
            speaker=SpeakerRef(role=speaker),
            direction=direction,
            content=Content(text=text),
        )
        items.append(event.model_dump(mode="json", exclude_none=True))
    return items


def niadra_operation(
    path: str,
    base: str,
    key: str,
    pairs: Sequence[tuple[Case, Identities]],
    tag: str,
    turns: int,
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> Operation:
    headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
    url = f"{base.rstrip('/')}/v1/batch"
    prefix = f"bench-{tag}-ing-{path}"
    # One counter for the warm-ups and every rate: each call is a new exchange, `turns` per conversation.
    sequence = itertools.count()

    async def call(client: httpx.AsyncClient, _n: int) -> tuple[float, str]:
        seq = next(sequence)
        case, ids = pairs[(seq // turns) % len(pairs)]
        items = niadra_items(case, ids, f"{prefix}-conv-{seq // turns}", seq, prefix)
        return await timed(client.post(url, json={"items": items}, headers=headers), 200)

    return Operation(latency.Probe("niadra", path, call, transport), "batch", "POST /v1/batch")


def mem0_operation(
    target: Mem0RestTarget,
    pairs: Sequence[tuple[Case, Identities]],
    mode: str,
    turns: int,
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> Operation:
    url = f"{target.url.rstrip('/')}/memories"
    headers = target.headers
    sequence = itertools.count()

    async def call(client: httpx.AsyncClient, _n: int) -> tuple[float, str]:
        seq = next(sequence)
        case, ids = pairs[(seq // turns) % len(pairs)]
        customer, agent = exchange(case.language, seq)
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": customer}, {"role": "assistant", "content": agent}],
            "user_id": ids.mem0_user("known_id", "whatsapp"),
            "metadata": {"channel": "whatsapp"},
        }
        if mode == "raw":
            payload["infer"] = False
        return await timed(client.post(url, json=payload, headers=headers), 200)

    call_name = "POST /memories infer=false" if mode == "raw" else "POST /memories"
    return Operation(latency.Probe("mem0_oss", "cluster", call, transport), f"add_{mode}", call_name)


async def run(
    niadra: NiadraTarget | None,
    mem0: Mem0RestTarget | None,
    pairs: Sequence[tuple[Case, Identities]],
    tag: str,
    settings: IngestSettings,
    *,
    rates: Sequence[int],
    duration_s: float,
    cooldown_s: float = 0.0,
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
    mem0_transport: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> list[dict[str, Any]]:
    turns = settings.turns_per_conversation
    ops: list[Operation] = []
    if niadra is not None:
        _, key = niadra.keys.for_channel("whatsapp")
        where = paths(niadra.client("whatsapp").base_url, "NIADRA_CLUSTER_INGEST_URL")
        for path, base in where.items():
            ops.append(niadra_operation(path, base, key, pairs, tag, turns, transport))
    out = await measure(ops, rates, duration_s, len(pairs), settings.request_timeout_s)
    if mem0 is not None:
        # raw before infer, and a pause after each infer rate: the infer loop leaves Mem0's server busy
        # with requests the client gave up on, which must not slow the next line down.
        for mode in sorted(settings.mem0_modes, key=("raw", "infer").index):
            op = mem0_operation(mem0, pairs, mode, turns, mem0_transport)
            for rate in rates:
                out += await measure([op], [rate], duration_s, len(pairs), settings.request_timeout_s)
                if mode == "infer" and cooldown_s:
                    log.info("ingest: waiting %ss for Mem0's server to finish its infer backlog", cooldown_s)
                    await asyncio.sleep(cooldown_s)
    return out
