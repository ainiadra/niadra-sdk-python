"""With Niadra down, the agent goes on: the chaos test of the SDK in the agent's process.

A conversation runs one good turn, then Niadra goes down in one of four ways while three more turns run, 1.5 s
apart, then it comes back. The SDK talks to Niadra through `tests/chaos/proxy.py`, a process of its own:

- `killed`: the proxy process gets SIGKILL, so the SDK's connections are reset and new ones refused;
- `blackhole`: requests are taken and never answered;
- `503`: every request is answered 503;
- `slow`: every request reaches Niadra, and its answer comes back after the deadline.

For each, during the outage:

- a turn (a read, then two checks before outbound contacts) ends within the SDK's documented budgets, 300 ms
  for a chat read and 200 ms for each check;
- the read serves the last good pack, marked `degraded` with its age, never an empty one;
- the customer's opt-out of marketing and of service, recorded before the conversation, holds: both checks
  deny with `suppressed`, and nothing goes out;

and once Niadra is back, every turn record, event and declaration made during the outage arrives, each stored
once. The proxy's log is the witness: what reached Niadra and what Niadra took.

Without a cell, Niadra is `niadra-mock` served over HTTP in this process. `tests/chaos/harness.py` says how to
point it at a cell instead. `NIADRA_CHAOS_REPORT` names a file that gets one JSON line of measures per run.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from niadra import AsyncNiadra, Niadra
from niadra._transport import RetryState
from niadra.coordination.client import CHECK_BUDGET
from niadra.models.coordination import CheckResult
from niadra.models.results import Context
from niadra.options import CacheOptions, Timeouts
from tests.chaos.harness import CellUpstream, Ledger, MockUpstream, Proxy, upstream

MODES = ("killed", "blackhole", "503", "slow")
OUTAGE_TURNS = 3
SLOW_S = 6.0
"""How late a `slow` answer comes: past every budget, the 5 s of a background write included."""
TURN_GAP_S = 1.5
"""The customer's pause between turns: an outage of seconds, past the retries of a single request."""
OPTED_OUT = ("marketing", "service")
READ_BUDGET = Timeouts().context
TURN_BUDGET = READ_BUDGET + len(OPTED_OUT) * CHECK_BUDGET
SLACK = 0.05
"""Scheduling on a loaded test machine, per call."""
DRAIN_TIMEOUT = 90.0

_backoff = RetryState._backoff


@pytest.fixture(autouse=True)
def _real_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retries of a real client, not the conftest's instant ones: the outage must cost what it costs."""
    monkeypatch.setattr(RetryState, "_backoff", _backoff)


@pytest.fixture
def niadra_side() -> Iterator[MockUpstream | CellUpstream]:
    side = upstream()
    side.prepare(OPTED_OUT)
    yield side
    side.close()


@pytest.fixture
def proxy(niadra_side: MockUpstream | CellUpstream, tmp_path: Path) -> Iterator[Proxy]:
    running = Proxy(niadra_side.url, tmp_path / "requests.jsonl")
    running.start()
    yield running
    running.stop()


@dataclass
class Turn:
    seconds: float
    read_seconds: float
    check_seconds: list[float]
    context: Context
    decisions: dict[str, CheckResult]
    turn_id: str


@dataclass
class Outage:
    turns: list[Turn] = field(default_factory=list)
    contacts: list[str] = field(default_factory=list)
    """Outbound contacts the agent made: only on an `allow`."""


def _down(proxy: Proxy, mode: str) -> None:
    if mode == "killed":
        proxy.kill()
    else:
        proxy.mode(mode, SLOW_S if mode == "slow" else 0.0)


def _up(proxy: Proxy, mode: str) -> None:
    if mode == "killed":
        proxy.start()
    else:
        proxy.mode("up")


def _client_options(side: MockUpstream | CellUpstream, proxy: Proxy) -> dict[str, Any]:
    # Every read goes to the network, so every outage turn takes the failing path; with the default cache a
    # conversation's turns inside 10 minutes are served from memory at once.
    return {
        "base_url": proxy.url,
        "channel": "whatsapp",
        "cache": CacheOptions(ttl=0, stale_while_revalidate=0),
    }


def _customer_line(n: int) -> str:
    return f"Turno {n}: e a troca do pedido 4471?"


def _judge(outage: Outage, good: Context, side: MockUpstream | CellUpstream) -> None:
    for turn in outage.turns:
        assert turn.read_seconds <= READ_BUDGET + SLACK, turn.read_seconds
        assert all(s <= CHECK_BUDGET + SLACK for s in turn.check_seconds), turn.check_seconds
        assert turn.seconds <= TURN_BUDGET + 3 * SLACK, turn.seconds
        context = turn.context
        assert context.text == good.text and context.text, "the last good pack, never an empty one"
        assert context.origin == "last_good" and context.degraded, context.origin
        assert context.age_ms is not None and context.age_ms > 0, context.age_ms
        for purpose, decision in turn.decisions.items():
            assert (decision.decision, decision.reasons) == ("deny", ["suppressed"]), (purpose, decision)
    assert not outage.contacts
    ages = [t.context.age_ms or 0 for t in outage.turns]
    assert ages == sorted(ages), "the age grows while Niadra is down"


def _wait_drained(
    log: Path, since: float, turn_ids: set[str], events: int, declarations: int
) -> tuple[float, Ledger]:
    """Seconds from `since` until the last write made during the outage reached Niadra, and the log then."""
    deadline = time.monotonic() + DRAIN_TIMEOUT
    while True:
        ledger = Ledger.read(log)
        after = Ledger.read(log, since)
        done = (
            turn_ids <= ledger.turn_ids()
            and len(ledger.event_keys()) >= events
            and len(ledger.declaration_keys()) >= declarations
        )
        if done:
            last = after.last(lambda e: e.get("forwarded") and 200 <= e.get("status", 0) < 300)
            return (last - since if last is not None else 0.0), ledger
        if time.monotonic() > deadline:
            raise AssertionError(
                f"not drained in {DRAIN_TIMEOUT:.0f} s: turns {len(ledger.turn_ids())}/{len(turn_ids)}, "
                f"events {len(ledger.event_keys())}/{events}, declarations {len(ledger.declaration_keys())}"
            )
        time.sleep(0.05)


def _account(ledger: Ledger, turn_ids: set[str], events: int, declarations: int) -> None:
    """Each write stored once: Niadra's own counts over every request that reached it."""
    assert ledger.turn_ids() == turn_ids
    assert ledger.turns_accepted() == len(turn_ids), ledger.turns_accepted()
    assert len(ledger.event_keys()) == events
    assert ledger.events_accepted() == events, ledger.events_accepted()
    assert len(ledger.declaration_keys()) == declarations


def _report(client: str, mode: str, outage: Outage, drain_s: float, ledger: Ledger) -> None:
    measures = {
        "sdk": "python",
        "client": client,
        "mode": mode,
        "turn_ms": [round(t.seconds * 1000, 1) for t in outage.turns],
        "read_ms": [round(t.read_seconds * 1000, 1) for t in outage.turns],
        "age_ms": [round(t.context.age_ms or 0) for t in outage.turns],
        "drain_s": round(drain_s, 2),
        "resent_duplicates": ledger.duplicates(),
    }
    print(json.dumps(measures))
    if path := os.environ.get("NIADRA_CHAOS_REPORT"):
        with open(path, "a", encoding="utf-8") as out:
            out.write(json.dumps(measures) + "\n")


def _sync_turn(conversation: Any, n: int) -> Turn:
    started = time.perf_counter()
    conversation.customer(_customer_line(n))
    with conversation.turn() as frame:
        read = time.perf_counter()
        context = conversation.context()
        read_seconds = time.perf_counter() - read
        decisions, checks = {}, []
        for purpose in OPTED_OUT:
            at = time.perf_counter()
            decisions[purpose] = conversation.check("follow_up", purpose=purpose, channel="whatsapp")
            checks.append(time.perf_counter() - at)
        conversation.agent(f"Resposta {n}: a troca do 4471 segue aberta.")
    return Turn(time.perf_counter() - started, read_seconds, checks, context, decisions, frame.turn_id)


async def _async_turn(conversation: Any, n: int) -> Turn:
    started = time.perf_counter()
    conversation.customer(_customer_line(n))
    with conversation.turn() as frame:
        read = time.perf_counter()
        context = await conversation.context()
        read_seconds = time.perf_counter() - read
        decisions, checks = {}, []
        for purpose in OPTED_OUT:
            at = time.perf_counter()
            decisions[purpose] = await conversation.check("follow_up", purpose=purpose, channel="whatsapp")
            checks.append(time.perf_counter() - at)
        conversation.agent(f"Resposta {n}: a troca do 4471 segue aberta.")
    return Turn(time.perf_counter() - started, read_seconds, checks, context, decisions, frame.turn_id)


def _receipt(key: str) -> dict[str, Any]:
    """The check before a receipt goes out: one effect, reserved by its key."""
    return {"intent": "receipt", "purpose": "transactional", "channel": "whatsapp", "effect_key": key}


def _reserved(decision: CheckResult) -> None:
    assert decision.decision == "allow", decision
    assert decision.effect is not None and decision.effect.state == "none", decision


def _declared(decision: CheckResult) -> None:
    """The receipt's declaration, made during the outage, reached Niadra: it is never sent again."""
    assert decision.effect is not None and decision.effect.state == "done", decision


def _contacts(turn: Turn, outage: Outage) -> None:
    outage.contacts += [p for p, d in turn.decisions.items() if d.decision == "allow"]


def _wait_for_copy(client: Niadra | AsyncNiadra) -> None:
    """The local copy of the suppression list, read in the background after the first check."""
    deadline = time.monotonic() + 10
    while not client._suppressions.held:
        assert time.monotonic() < deadline, "the checks never read the suppression list"
        time.sleep(0.02)


@pytest.mark.parametrize("mode", MODES)
def test_the_agent_goes_on_with_niadra_down(
    mode: str, niadra_side: MockUpstream | CellUpstream, proxy: Proxy
) -> None:
    client = Niadra(niadra_side.key, **_client_options(niadra_side, proxy))
    receipt = f"receipt:{uuid.uuid4().hex[:12]}"
    outage = Outage()
    conversation_id = f"chaos-{mode}-{uuid.uuid4().hex[:8]}"
    with client.conversation(conversation_id, subject=niadra_side.subject) as conversation:
        first = _sync_turn(conversation, 0)
        good = first.context
        assert good.origin == "network" and good.text and good.age_ms == 0
        assert all(d.reasons == ["suppressed"] for d in first.decisions.values()), first.decisions
        reserved = conversation.check(**_receipt(receipt))
        _reserved(reserved)
        _wait_for_copy(client)
        assert client.flush(10)
        since = time.time()

        _down(proxy, mode)
        # The receipt left before the outage; its declaration is made now and waits in the outbox.
        conversation.declare.effect(receipt, "done")
        for n in range(1, OUTAGE_TURNS + 1):
            time.sleep(TURN_GAP_S)
            turn = _sync_turn(conversation, n)
            outage.turns.append(turn)
            _contacts(turn, outage)
        assert not client.may_contact(niadra_side.subject, "marketing", channel="whatsapp")
        assert not client.may_contact(niadra_side.subject, "service", channel="whatsapp")
        _judge(outage, good, niadra_side)

        restored = time.time()
        _up(proxy, mode)
        turn_ids = {first.turn_id, *(t.turn_id for t in outage.turns)}
        events = 2 * (1 + OUTAGE_TURNS)
        drain_s, ledger = _wait_drained(proxy.log, restored, turn_ids, events, 1)
        _declared(conversation.check(**_receipt(receipt)))
    client.close()
    assert since < restored
    _account(ledger, turn_ids, events, 1)
    _report("sync", mode, outage, drain_s, ledger)


@pytest.mark.parametrize("mode", MODES)
async def test_the_async_agent_goes_on_with_niadra_down(
    mode: str, niadra_side: MockUpstream | CellUpstream, proxy: Proxy
) -> None:
    client = AsyncNiadra(niadra_side.key, **_client_options(niadra_side, proxy))
    receipt = f"receipt:{uuid.uuid4().hex[:12]}"
    outage = Outage()
    conversation_id = f"chaos-async-{mode}-{uuid.uuid4().hex[:8]}"
    async with client.conversation(conversation_id, subject=niadra_side.subject) as conversation:
        first = await _async_turn(conversation, 0)
        good = first.context
        assert good.origin == "network" and good.text and good.age_ms == 0
        _reserved(await conversation.check(**_receipt(receipt)))
        deadline = time.monotonic() + 10
        while not client._suppressions.held:
            assert time.monotonic() < deadline, "the checks never read the suppression list"
            await asyncio.sleep(0.02)
        assert await client.flush(10)

        await asyncio.to_thread(_down, proxy, mode)
        conversation.declare.effect(receipt, "done")
        for n in range(1, OUTAGE_TURNS + 1):
            await asyncio.sleep(TURN_GAP_S)
            turn = await _async_turn(conversation, n)
            outage.turns.append(turn)
            _contacts(turn, outage)
        assert not await client.may_contact(niadra_side.subject, "marketing", channel="whatsapp")
        assert not await client.may_contact(niadra_side.subject, "service", channel="whatsapp")
        _judge(outage, good, niadra_side)

        restored = time.time()
        await asyncio.to_thread(_up, proxy, mode)
        turn_ids = {first.turn_id, *(t.turn_id for t in outage.turns)}
        events = 2 * (1 + OUTAGE_TURNS)
        drain_s, ledger = await asyncio.to_thread(_wait_drained, proxy.log, restored, turn_ids, events, 1)
        _declared(await conversation.check(**_receipt(receipt)))
    await client.close()
    _account(ledger, turn_ids, events, 1)
    _report("async", mode, outage, drain_s, ledger)
