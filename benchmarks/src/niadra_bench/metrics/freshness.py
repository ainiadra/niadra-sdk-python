"""Metric 6: how long a turn said on one channel takes to be readable on another.

Each trial is a new customer. The customer writes on WhatsApp a message with a fresh number; the clock
starts when the application hands the write to the memory (`track()` for Niadra, `add()` for Mem0)
and stops at the first read, from the voice agent's side, whose text holds the number: Niadra's
`context(view="voice")` in another conversation, verified at V1 before the clock starts, Mem0's
`search()` with the same user id.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from niadra import Content, EventItem, Speaker, SpeakerRef

from niadra_bench.dataset.model import Case
from niadra_bench.identity import Identities
from niadra_bench.stats import distribution
from niadra_bench.targets.mem0 import Mem0RestTarget
from niadra_bench.targets.niadra import NiadraTarget
from niadra_bench.text import contains

MESSAGES = {
    "pt": "Oi, o número do meu pedido novo é {code}, pode anotar?",
    "en": "Hi, my new order number is {code}, can you note it?",
}
QUERIES = {"pt": "número do pedido novo", "en": "new order number"}


async def _poll(
    read: Callable[[], Awaitable[str]], code: str, interval_s: float, timeout_s: float
) -> float | None:
    """Seconds until a read shows the code, or None past the timeout. The clock is started by the caller."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if contains(await read(), code):
            return time.perf_counter()
        await asyncio.sleep(interval_s)
    return None


async def niadra_trial(
    target: NiadraTarget, case: Case, ids: Identities, interval_s: float, timeout_s: float
) -> float | None:
    code = str(random.randint(100000, 999999))
    writer = target.client("whatsapp")
    reader = target.client("voice")
    event = EventItem(
        kind="message",
        channel="whatsapp",
        conversation_id=f"bench-{ids.tag}-fresh-{case.id}-wa",
        handles=[ids.channel_handle("whatsapp")],
        speaker=SpeakerRef(role=Speaker.CUSTOMER),
        direction="inbound",
        content=Content(text=MESSAGES[case.language].format(code=code)),
        verification_hint="V1",
    )
    voice = f"bench-{ids.tag}-fresh-{case.id}-voice"
    # The voice agent proves the caller first, as in every probe of the accuracy pass: an unproven call
    # reads at V0, where the starter policy withholds order numbers, and the message would never show.
    await reader.verify(
        "network_attestation", "V1", handle=ids.channel_handle("voice"), conversation_id=voice
    )

    async def read() -> str:
        context = await reader.context(
            ids.channel_handle("voice"),
            view="voice",
            verification="V1",
            conversation_id=voice,
            use_cache=False,
            timeout=5.0,
        )
        return "\n".join((context.system_block, context.turn_block))

    started = time.perf_counter()
    writer.track(event)
    seen = await _poll(read, code, interval_s, timeout_s)
    return None if seen is None else (seen - started) * 1000


async def mem0_trial(
    target: Mem0RestTarget, case: Case, ids: Identities, interval_s: float, timeout_s: float
) -> float | None:
    code = str(random.randint(100000, 999999))
    user = ids.mem0_user("known_id", "whatsapp")

    async def read() -> str:
        results, _ = await target.search(user, QUERIES[case.language])
        return "\n".join(str(r.get("memory") or "") for r in results)

    started = time.perf_counter()
    await target._add(
        {
            "messages": [{"role": "user", "content": MESSAGES[case.language].format(code=code)}],
            "user_id": user,
            "metadata": {"channel": "whatsapp"},
        }
    )
    seen = await _poll(read, code, interval_s, timeout_s)
    return None if seen is None else (seen - started) * 1000


async def run(
    niadra: NiadraTarget | None,
    mem0: Mem0RestTarget | None,
    cases: list[Case],
    tag: str,
    trials: int,
    interval_ms: int,
    timeout_s: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    picked = [cases[i % len(cases)] for i in range(trials)]
    interval = interval_ms / 1000
    for system, target in (("niadra", niadra), ("mem0_oss", mem0)):
        if target is None:
            continue
        samples: list[float] = []
        timeouts = 0
        for n, case in enumerate(picked):
            ids = Identities.for_case(case, f"{tag}f{n}")
            if isinstance(target, NiadraTarget):
                ms = await niadra_trial(target, case, ids, interval, timeout_s)
            else:
                ms = await mem0_trial(target, case, ids, interval, timeout_s)
            if ms is None:
                timeouts += 1
            else:
                samples.append(ms)
        out.append(
            {
                "system": system,
                "trials": trials,
                "timeouts": timeouts,
                "timeout_s": timeout_s,
                **distribution(samples),
            }
        )
    return out
