"""The tool counterfactual in the company's CI (`niadra.replay.counterfactual`): recorded calls of a tool run
again with and without one element of the constraints block, and only positions and overlaps reach Niadra."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
import pytest

from niadra import Niadra, phone
from niadra.errors import NiadraError
from niadra.options import CacheOptions, TurnOptions
from niadra.replay import Counterfactual
from niadra.turns import tool
from niadra_mock import MOCK_KEY, MockApp

CUSTOMER = phone("+5511912345678")
CATALOG = [
    {"variant_id": str(n), "color": color, "price": price}
    for n, (color, price) in enumerate(
        [("red", 120), ("blue", 90), ("red", 80), ("black", 150), ("blue", 60), ("green", 70), ("red", 40)]
    )
]
SEARCH = {
    "tool": "search_products",
    "args": [{"attr": "item_variant.color", "param": "color", "negation": {"param": "not_color"}}],
    "results": [
        {
            "path": "cards[*]",
            "type": "item_variant",
            "namespace": "store",
            "id": "variant_id",
            "fields": {"color": "color"},
        }
    ],
    "capabilities": {"overfetch": False, "dry_run_param": "dry_run"},
}
BLOCK = {
    "version": "cv_0123456789abcdef",
    "hard": [
        {
            "id": "h1",
            "attr": "item_variant.color",
            "op": "not_in",
            "values": ["red"],
            "source": "stated",
            "scope": "session",
            "origin": {"kind": "stated"},
        }
    ],
}
ran: list[dict[str, Any]] = []


@tool("search_products", binding=SEARCH, dry_run=True)
def search_products(not_color: list[str] | str | None = None, color: str | None = None) -> dict[str, Any]:
    ran.append({"not_color": not_color, "color": color})
    refused = {not_color} if isinstance(not_color, str) else set(not_color or ())
    cards = [c for c in CATALOG if c["color"] not in refused and (color is None or c["color"] == color)]
    return {"cards": sorted(cards, key=lambda c: c["price"])}


def reserve(not_color: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    ran.append({"reserve": not_color, "dry_run": dry_run})
    return {"cards": [c for c in CATALOG if c["color"] != not_color]}


@pytest.fixture
def app() -> MockApp:
    app = MockApp()
    app.cell.features.update({"signals", "measurement"})
    app.cell.agent_features.constrain(CUSTOMER, BLOCK)
    return app


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


def _recorded(niadra: Niadra, call: Any, *, engaged: str | None = None) -> str:
    """A turn that read the block, searched with it rendered, and showed the result: the person engaged."""
    build = niadra.build(prompts={"core": "v16"}, model="model-a")
    with niadra.conversation(f"c-{uuid4().hex[:6]}", subject=CUSTOMER, agent_id="stylist") as conversation:
        conversation.customer("Quero um vestido, mas não vermelho.")
        with conversation.turn(build=build) as frame:
            conversation.context(include=["constraints"])
            found = call()
            exposure = str(uuid4())
            frame.interact(
                {
                    "kind": "presented",
                    "exposure_id": exposure,
                    "list_id": "l1",
                    "list_kind": "search_products",
                    "delivered_at": datetime.now(timezone.utc).isoformat(),
                    "visible_k": 3,
                    "items": [
                        {"pos": i + 1, "ref": f"item_variant:store:{c['variant_id']}"}
                        for i, c in enumerate(found["cards"])
                    ],
                }
            )
            if engaged is not None:
                frame.interact({"kind": "engaged", "exposure_id": exposure, "ref": engaged, "how": "click"})
            conversation.agent("Separei três opções que não são vermelhas.")
    assert niadra.flush(5)
    return frame.turn_id


def test_the_hard_element_moves_the_list_beyond_the_tool_noise(app: MockApp, niadra: Niadra) -> None:
    turn = _recorded(niadra, lambda: search_products(not_color=["red"]), engaged="item_variant:store:5")
    ran.clear()
    run = Counterfactual(niadra, {"search_products": search_products}).run(
        [turn], tool="search_products", element="hard", label="abc123"
    )
    assert ran == [{"not_color": ["red"], "color": None}] * 2 + [{"not_color": None, "color": None}]
    (case,) = run.cases
    assert case["status"] == "completed" and case["dry_run"] is False
    assert (case["k"], case["noise"], case["base_count"], case["variant_count"]) == (3, 1.0, 4, 7)
    assert case["overlap"] < 1.0
    assert case["engaged"] == [{"base": 2, "variant": 3}]
    report = run.report
    assert report["effect"] == pytest.approx(1.0 - case["overlap"], abs=1e-6)
    assert {"not_quality", "trivial_for_hard", "few_cases"} <= set(report["limits"])
    sent = json.dumps(run.cases)
    assert "red" not in sent and "store:5" not in sent, "positions and overlaps only"


def test_a_call_the_element_did_not_touch_is_no_case(app: MockApp, niadra: Niadra) -> None:
    touched = _recorded(niadra, lambda: search_products(not_color=["red"]))
    untouched = _recorded(niadra, lambda: search_products(color="blue"))
    run = Counterfactual(niadra, {"search_products": search_products}).run(
        [touched, untouched], tool="search_products", element="hard"
    )
    assert len(run.cases) == 1 and run.untouched == 1


def test_a_tool_that_is_not_safe_runs_dry_or_not_at_all(app: MockApp, niadra: Niadra) -> None:
    bound = tool("reserve", binding={**SEARCH, "tool": "reserve"})(reserve)
    turn = _recorded(niadra, lambda: bound(not_color="red"))
    ran.clear()
    run = Counterfactual(
        niadra, {"reserve": reserve}, bindings={"reserve": {**SEARCH, "tool": "reserve"}}
    ).run([turn], tool="reserve", element="hard")
    assert run.cases[0]["dry_run"] is True
    assert all(r["dry_run"] for r in ran)
    no_dry = {**SEARCH, "tool": "reserve", "capabilities": {}}
    ran.clear()
    run = Counterfactual(niadra, {"reserve": reserve}, bindings={"reserve": no_dry}).run(
        [turn], tool="reserve", element="hard"
    )
    assert run.cases == [{"turn_id": turn, "call_id": "k1", "status": "no_dry_run", "dry_run": False}]
    assert ran == []
    assert run.report["skipped"] == {"no_dry_run": 1}


def test_a_failing_tool_and_nothing_to_report(app: MockApp, niadra: Niadra) -> None:
    turn = _recorded(niadra, lambda: search_products(not_color=["red"]))

    def broken(**_: Any) -> dict[str, Any]:
        raise RuntimeError("down")

    run = Counterfactual(
        niadra, {"search_products": broken}, bindings={"search_products": SEARCH}, safe=["search_products"]
    ).run([turn], tool="search_products", element="hard")
    assert run.cases[0]["status"] == "tool_error"
    with pytest.raises(NiadraError):
        Counterfactual(niadra, {"search_products": search_products}).run(
            [turn], tool="search_products", element="size"
        )


TOOLS = {"search_products": search_products}


def test_the_command_prints_the_report(
    app: MockApp, niadra: Niadra, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from niadra import cli as command

    turn = _recorded(niadra, lambda: search_products(not_color=["red"]))
    monkeypatch.setattr(command, "Niadra", lambda: niadra)
    argv = ["counterfactual", "--tools", "tests.test_counterfactual:TOOLS", "--tool", "search_products"]
    assert command.main([*argv, "--element", "hard", "--turn", turn, "--label", "abc"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["label"], report["completed"], report["untouched"]) == ("abc", 1, 0)
    assert command.main([*argv, "--element", "size", "--turn", turn]) == 2


def test_a_bound_call_records_what_its_result_honored(app: MockApp, niadra: Niadra) -> None:
    turn = _recorded(niadra, lambda: search_products(not_color=["red"]), engaged="item_variant:store:5")
    record = app.cell.turns.turns[turn].record
    assert record["calls"][0]["applied"] == {
        "constraints": "cv_0123456789abcdef",
        "hard_sent": ["h1"],
        "results_checked": 4,
        "violations": 0,
        "unverifiable": 0,
    }
    leaky = _recorded(niadra, lambda: search_products(color=None))
    applied = app.cell.turns.turns[leaky].record["calls"][0]["applied"]
    assert (applied["hard_sent"], applied["violations"]) == ([], 0), "nothing sent, nothing measured against"
    assert [i["kind"] for i in record["interactions"]] == ["presented", "engaged"]
