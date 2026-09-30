"""The concept examples (`examples/`) run as the documentation shows them, against the emulator: the claim
guard, coordination, object state, the working state, a masked tool output, the tool counterfactual, and the
CI workflow's commands."""

from __future__ import annotations

import importlib.util
import json
import shlex
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import httpx
import pytest

from niadra import Niadra, phone
from niadra.options import CacheOptions, TurnOptions
from niadra.turns.mask import MASKED, WITHHELD
from niadra_mock import MOCK_KEY, MockApp

ROOT = Path(__file__).resolve().parents[1]
CUSTOMER = phone("+5511912345678")
RETAIL = json.loads((ROOT / "spec" / "examples" / "claim-contract" / "retail.json").read_text())


def _example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"example_{name}", ROOT / "examples" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app() -> MockApp:
    mock = MockApp()
    mock.cell.features.update({"coordination", "state", "agent_state", "signals", "measurement"})
    return mock


def _client(app: MockApp, channel: str = "whatsapp") -> Niadra:
    return Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel=channel,
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=httpx.Client(transport=httpx.WSGITransport(app=app.wsgi)),
        turns=TurnOptions(interval=3600),
    )


@pytest.fixture
def niadra(app: MockApp) -> Iterator[Niadra]:
    client = _client(app)
    yield client
    client.close()


def test_the_claim_guard_flags_a_wrong_price_and_holds_back_the_prompt(app: MockApp, niadra: Niadra) -> None:
    example = _example("claim_guard")
    niadra.use_claim_contract(
        {
            **RETAIL,
            "internal_text": {"shingle_hashes_ref": "prompts@v16", "n": 8, "redact": "(instrução interna)"},
        }
    )
    with niadra.conversation("c-1", subject=CUSTOMER, agent_id="store") as conversation:
        wrong = example.guarded_reply(niadra, conversation, "O vestido sai por R$ 199,90 hoje. Quer levar?")
        leak = "Claro! Never offer a discount above ten percent without the manager's approval. Posso ajudar?"
        held = example.guarded_reply(niadra, conversation, leak)
    assert wrong == "O vestido sai por R$ 199,90 hoje. Quer levar?"
    assert held == "Claro! (instrução interna). Posso ajudar?"
    assert niadra.flush(5)
    records = [stored.record for stored in app.cell.turns.turns.values()]
    found = [(c["verdict"], c["action"]) for r in records for c in r["claims"]]
    assert found == [("mismatch", "warn"), ("internal_text_found", "block")]
    assert "discount" not in json.dumps(records)


def test_the_farewell_goes_once(niadra: Niadra) -> None:
    example = _example("coordination")
    sent: list[str] = []
    with niadra.conversation("c-2", subject=CUSTOMER, agent_id="closing") as conversation:
        with conversation.turn():
            assert example.farewell_once(conversation, sent.append) is True
        assert niadra.flush(5)
        with conversation.turn():
            assert example.farewell_once(conversation, sent.append) is False
    assert len(sent) == 1


def test_a_stale_price_is_read_again_before_it_is_said(app: MockApp, niadra: Niadra) -> None:
    example = _example("object_state")
    with niadra.conversation("c-3", subject=CUSTOMER, agent_id="sales") as conversation, conversation.turn():
        app.cell.state.observe(example.QUOTE, {"price_full": 511.06})
        assert example.price_line(conversation, 511.06) == "O plano sai por R$ 511.06."
        app.cell.state.observe(example.QUOTE, {"price_full": 511.06}, status="stale")
        assert example.price_line(conversation, 511.06) == "Vou confirmar o valor atualizado e já te digo."
        niadra.resolvers.register("health_quote", example.requote)
        assert example.price_line(conversation, 511.06) == "O plano sai por R$ 499.90."


def test_two_sub_agents_keep_each_others_fields(niadra: Niadra) -> None:
    example = _example("working_state")
    with niadra.conversation("c-4", subject=CUSTOMER, agent_id="sales") as conversation:
        example.remember_offer(conversation, "accepted")
        state = example.remember_address(conversation, "Campinas")
    assert state == {"offer": {"status": "accepted"}, "delivery": {"city": "Campinas"}}


def test_the_model_gets_the_proposal_as_the_key_may_read_it(app: MockApp) -> None:
    example = _example("masked_tool")
    app.cell.agent_features.declare(
        {
            "type": "proposal",
            "ownership": "subject",
            "mirror_of": {"system": "crm"},
            "fields": {"price_full": {"type": "money"}, "health_declaration": {"type": "text"}},
            "field_access": {"health_declaration": "mask"},
        }
    )
    client = _client(app, "voice")
    try:
        assert example.proposal_tool(client)("p-19") == WITHHELD, "no profile read yet: withheld"
        client.profile()
        got = example.proposal_tool(client)("p-19")
    finally:
        client.close()
    assert got == {"proposal_id": "p-19", "price_full": 812.4, "health_declaration": MASKED}


def test_the_counterfactual_of_the_hard_constraints(app: MockApp, niadra: Niadra) -> None:
    example = _example("tool_counterfactual")
    # The binding the example's docstring shows, declared by the space and served in the SDK profile.
    app.cell.agent_features.tool_bindings = [
        {
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
        }
    ]
    niadra.profile()
    app.cell.agent_features.constrain(
        CUSTOMER,
        {
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
        },
    )
    with niadra.conversation("c-5", subject=CUSTOMER, agent_id="stylist") as conversation:
        conversation.customer("Um vestido, mas não vermelho.")
        with conversation.turn(build=niadra.build(prompts={"stylist": "v1"}, model="model-a")) as frame:
            conversation.context(include=["constraints"])
            cards = example.search_products(not_color=["red"])["cards"]
            items = [
                {"pos": i + 1, "ref": f"item_variant:store:{c['variant_id']}"} for i, c in enumerate(cards)
            ]
            frame.interact(
                {
                    "kind": "presented",
                    "exposure_id": str(uuid4()),
                    "list_id": "l1",
                    "list_kind": "search_products",
                    "delivered_at": datetime.now(timezone.utc).isoformat(),
                    "visible_k": 3,
                    "items": items,
                }
            )
            conversation.agent("Separei opções que não são vermelhas.")
    assert niadra.flush(5)
    report = example.measure(niadra, [frame.turn_id], label="abc123")
    assert (report["cases"], report["untouched"], report["completed"]) == (1, 0, 1)


def test_the_ci_workflow_runs_commands_the_cli_takes() -> None:
    from niadra.cli import parser

    lines = (ROOT / "examples" / "ci" / "niadra-checks.yml").read_text().splitlines()
    commands = [line.split("run: ", 1)[1] for line in lines if line.strip().startswith("run: niadra ")]
    assert len(commands) == 2
    for command in commands:
        parser().parse_args(shlex.split(command)[1:])
