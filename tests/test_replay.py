"""Replay inside the company's boundary (`niadra.replay`): a recorded turn runs again N times with its tools
answered from the record, its assertions are evaluated here, and Niadra decides the verdict."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from niadra import AsyncNiadra, Niadra, phone
from niadra.options import CacheOptions, TurnOptions
from niadra.replay import AsyncReplayer, Replayer, ReplayInput
from niadra.turns import tool
from niadra_mock import MOCK_KEY, MockApp

CUSTOMER = phone("+5511912345678")
calls: list[str] = []


@tool("quote")
def quote(plan: str) -> dict[str, Any]:
    calls.append(plan)
    return {"plan": plan, "price_full": 511.06}


@tool("stock", dry_run=True)
def stock(sku: str) -> dict[str, Any]:
    calls.append(sku)
    return {"sku": sku, "qty": 2}


def quoting(given: ReplayInput | None) -> str:
    found = quote("ouro")
    return f"O plano ouro sai por R$ {found['price_full']:.2f} no valor cheio."


def guessing(given: ReplayInput | None) -> str:
    return "O plano ouro sai por uns R$ 500."


@pytest.fixture
def app() -> MockApp:
    return MockApp()


@pytest.fixture
def niadra(app: MockApp) -> Iterator[Niadra]:
    client = Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="whatsapp",
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=httpx.Client(transport=httpx.WSGITransport(app=app.wsgi)),
        turns=TurnOptions(interval=3600),
    )
    yield client
    client.close()


def _recorded(app: MockApp, niadra: Niadra) -> str:
    build = niadra.build(prompts={"core": "v16"}, model="model-a")
    with niadra.conversation("c-1", subject=CUSTOMER, agent_id="sales") as conversation:
        conversation.customer("Quanto sai o plano ouro?")
        with conversation.turn(build=build) as frame:
            conversation.agent(quoting(None))
    assert niadra.flush(5)
    calls.clear()
    return frame.turn_id


def test_a_scenario_replayed_five_times_passes_and_its_tools_never_run(app: MockApp, niadra: Niadra) -> None:
    scenario = app.cell.replay.create({"name": "quote", "turn_ids": [_recorded(app, niadra)]})
    assert [a["id"] for a in scenario["assertions"]] == ["t1.tool_called.quote", "t1.budget"]
    seen: list[ReplayInput] = []

    def factory() -> Any:
        def agent(given: ReplayInput) -> str:
            seen.append(given)
            return quoting(given)

        return agent

    build = niadra.build(prompts={"core": "v17"}, model="model-a")
    run = Replayer(niadra, factory, build=build).run([scenario["scenario_id"]], runs=5, vary=["prompts"])
    assert (run.status, run.verdict) == ("done", "pass")
    (summary,) = run.scenarios
    assert summary["completed"] == 5 and summary["assertions"][0]["passed"] == 5
    assert calls == []  # every call answered from the record
    assert seen[0].text == "Quanto sai o plano ouro?" and [g.run for g in seen] == [0, 1, 2, 3, 4]


def test_an_agent_that_stops_calling_the_tool_regresses(app: MockApp, niadra: Niadra) -> None:
    scenario = app.cell.replay.create({"name": "quote", "turn_ids": [_recorded(app, niadra)]})
    ids = [scenario["scenario_id"]]
    build = niadra.build(prompts={"core": "v17"}, model="model-a")
    assert Replayer(niadra, lambda: quoting, build=build).run(ids, vary=["prompts"]).verdict == "pass"
    run = Replayer(niadra, lambda: guessing, build=build).run(ids, vary=["prompts"])
    assert run.verdict == "regression" and run.regressed
    tool_called = run.scenarios[0]["assertions"][1]
    assert tool_called["id"] == "t1.tool_called.quote"
    assert (tool_called["failed"], tool_called["baseline_passed"], tool_called["p_value"]) == (5, 5, 0.003968)


def test_a_pin_that_does_not_match_stops_the_case(app: MockApp, niadra: Niadra) -> None:
    scenario = app.cell.replay.create({"name": "quote", "turn_ids": [_recorded(app, niadra)]})
    build = niadra.build(prompts={"core": "v17"}, model="model-b")
    run = Replayer(niadra, lambda: quoting, build=build).run([scenario["scenario_id"]], vary=["prompts"])
    assert run.verdict == "pin_mismatch" and run.scenarios[0]["pin_mismatches"] == 5


def test_nothing_the_replayed_agent_sends_or_declares_reaches_niadra(app: MockApp, niadra: Niadra) -> None:
    scenario = app.cell.replay.create(
        {
            "name": "farewell",
            "turn_ids": [_recorded(app, niadra)],
            "assertions": [{"id": "once", "kind": "effect_once", "args": {"key": "farewell:c-1"}}],
        }
    )
    events = len(app.cell.events)

    def agent(given: ReplayInput) -> None:
        with niadra.conversation("c-1", subject=CUSTOMER, agent_id="sales") as conversation:
            context = conversation.context()
            assert context.error == "replay"
            decision = conversation.check("farewell", purpose="service", effect_key="farewell:c-1")
            if decision.decision == "allow":
                conversation.agent("Até logo!")
                conversation.declare.effect("farewell:c-1", "done")

    build = niadra.build(prompts={"core": "v16"}, model="model-a")
    run = Replayer(niadra, lambda: agent, build=build).run([scenario["scenario_id"]], runs=2)
    assert run.verdict == "pass" and run.scenarios[0]["assertions"][0]["passed"] == 2
    assert niadra.flush(5)
    assert len(app.cell.events) == events and not app.cell.coordination.declarations


def test_a_call_the_record_does_not_hold_runs_only_when_safe(app: MockApp, niadra: Niadra) -> None:
    scenario = app.cell.replay.create(
        {
            "name": "stock",
            "turn_ids": [_recorded(app, niadra)],
            "assertions": [{"id": "stock", "kind": "tool_called", "args": {"tool": "stock"}}],
        }
    )

    def agent(given: ReplayInput) -> str:
        quote("prata")  # other arguments: no recorded answer, and not safe to run again
        return str(stock("sku-9")["qty"])

    build = niadra.build(prompts={"core": "v16"}, model="model-a")
    run = Replayer(niadra, lambda: agent, build=build).run([scenario["scenario_id"]], runs=1)
    assert calls == ["sku-9"] and run.verdict == "pass"


def test_an_agent_that_raises_is_an_infrastructure_error(app: MockApp, niadra: Niadra) -> None:
    scenario = app.cell.replay.create({"name": "quote", "turn_ids": [_recorded(app, niadra)]})

    def broken(given: ReplayInput) -> str:
        raise TimeoutError

    build = niadra.build(prompts={"core": "v16"}, model="model-a")
    run = Replayer(niadra, lambda: broken, build=build).run([scenario["scenario_id"]], runs=3)
    assert run.verdict == "infrastructure_error" and run.scenarios[0]["infrastructure_errors"] == 3


def test_the_demo_agent_replays_with_a_known_verdict(app: MockApp, niadra: Niadra) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "examples" / "replay_demo.py"
    spec = importlib.util.spec_from_file_location("replay_demo", path)
    assert spec is not None and spec.loader is not None
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    turn_id = demo.record_turn(niadra, "c-demo", CUSTOMER)
    assert niadra.flush(5)
    scenario = app.cell.replay.create({"name": "demo", "turn_ids": [turn_id]})
    run = Replayer(niadra, demo.build_agent, build=demo.BUILD).run([scenario["scenario_id"]], runs=5)
    assert run.verdict == "pass" and run.scenarios[0]["completed"] == 5


async def test_the_async_runner_awaits_an_async_agent(app: MockApp, niadra: Niadra) -> None:
    turn_id = _recorded(app, niadra)
    scenario = app.cell.replay.create({"name": "quote", "turn_ids": [turn_id]})

    async def agent(given: ReplayInput) -> str:
        return quoting(given)

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.asgi))
    client = AsyncNiadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http)
    build = client.build(prompts={"core": "v16"}, model="model-a")
    run = await AsyncReplayer(client, lambda: agent, build=build).run([scenario["scenario_id"]])
    assert run.verdict == "pass" and calls == []
    await client.close(timeout=1)
    await http.aclose()
