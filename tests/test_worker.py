"""`niadra resolver-worker` and `niadra replay`: the command line the company runs itself."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from niadra import Niadra, phone
from niadra import cli as command
from niadra.cli.worker import ResolverWorker
from niadra.models.state import StateRef
from niadra.options import TurnOptions
from niadra.resolvers import Resolvers
from niadra.turns import tool
from niadra_mock import MOCK_KEY, MockApp

QUOTE = "health_quote:op:q-77"
RESOLVERS = Resolvers()
RESOLVERS.register("health_quote", lambda ref, fields: {"price_full": 499.9}, rate=100)
BUILD = {"prompts": {"core": "v16"}, "model": "model-a"}


@tool("quote")
def quote(plan: str) -> dict[str, Any]:
    return {"plan": plan, "price_full": 511.06}


def build_agent() -> Any:
    return lambda given: f"Sai por R$ {quote('ouro')['price_full']:.2f}."


@pytest.fixture
def app() -> MockApp:
    mock = MockApp()
    mock.cell.features.add("state")
    return mock


def _client(app: MockApp) -> Niadra:
    return Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="whatsapp",
        http_client=httpx.Client(transport=httpx.WSGITransport(app=app.wsgi)),
        turns=TurnOptions(interval=3600),
    )


def test_the_worker_reads_what_niadra_asks_for_and_pushes_it(app: MockApp) -> None:
    app.cell.state.observe(QUOTE, {"price_full": 511.06}, status="stale")
    app.cell.state.request_refresh(QUOTE)
    app.cell.state.request_refresh("item_variant:store:991")
    client = _client(app)
    client.resolvers = RESOLVERS
    worker = ResolverWorker(client)
    assert worker.run_once() == 1
    (pushed,) = app.cell.state.pushes
    assert pushed["fields"] == {"price_full": 499.9} and pushed["provenance"]["source"] == "live"
    assert [r["ref"]["type"] for r in app.cell.state.refreshes.values()] == ["item_variant"]
    assert client.verify_claim(QUOTE, "price_full", 499.9).claim_safe
    client.close()


def test_a_resolver_that_fails_leaves_the_request_to_its_lease(app: MockApp) -> None:
    def broken(ref: StateRef, fields: Sequence[str] | None) -> dict[str, Any]:
        raise RuntimeError("down")

    app.cell.state.request_refresh(QUOTE)
    client = _client(app)
    client.resolvers.register("health_quote", broken)
    worker = ResolverWorker(client)
    assert worker.run_once() == 0 and worker.skipped == 1
    assert len(app.cell.state.refreshes) == 1
    client.close()


def test_the_command_runs_the_worker_once(app: MockApp, monkeypatch: pytest.MonkeyPatch) -> None:
    app.cell.state.request_refresh(QUOTE)
    monkeypatch.setattr(command, "Niadra", lambda: _client(app))
    assert command.main(["resolver-worker", "--resolvers", "tests.test_worker:RESOLVERS", "--once"]) == 0
    assert app.cell.state.pushes and not app.cell.state.refreshes


def test_the_command_replays_and_exits_by_the_verdict(app: MockApp, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(app)
    with client.conversation("c-1", subject=phone("+5511912345678"), agent_id="sales") as conversation:
        conversation.customer("Quanto sai?")
        with conversation.turn(build=client.build(prompts={"core": "v16"}, model="model-a")) as frame:
            conversation.agent(build_agent()(None))
    assert client.flush(5)
    scenario = app.cell.replay.create({"name": "quote", "turn_ids": [frame.turn_id]})
    monkeypatch.setattr(command, "Niadra", lambda: client)
    arguments = ["replay", "--agent", "tests.test_worker:build_agent", "--build", "tests.test_worker:BUILD"]
    assert command.main([*arguments, "--scenario", scenario["scenario_id"], "--runs", "3"]) == 0
