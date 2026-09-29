"""The warm cache with Niadra up and down: the SDK profile, the blocks of `include`, the claim contract in
count mode, and the suppression list's local copy."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from niadra import AsyncNiadra, Niadra, phone
from niadra.models.state import ClaimContractSummary
from niadra.options import CacheOptions, TurnOptions
from niadra_mock import MOCK_KEY, MockApp

ROOT = Path(__file__).resolve().parents[1]
RETAIL = json.loads((ROOT / "spec" / "examples" / "claim-contract" / "retail.json").read_text())
CUSTOMER = phone("+5511912345678")
OTHER = phone("+5511998765432")


def _example() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "turn_records_example", ROOT / "examples" / "turn_records.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client(app: MockApp) -> Niadra:
    return Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="whatsapp",
        cache=CacheOptions(ttl=0, stale_while_revalidate=600),
        http_client=httpx.Client(transport=httpx.WSGITransport(app=app.wsgi)),
        turns=TurnOptions(interval=3600),
    )


@pytest.fixture
def store() -> MockApp:
    app = MockApp()
    app.cell.features.update({"signals", "coordination"})
    app.cell.agent_features.claim_contract = ClaimContractSummary.model_validate(RETAIL)
    app.cell.agent_features.constrain(
        CUSTOMER,
        {
            "version": "cv_0123456789abcdef",
            "hard": [
                {
                    "id": "h1",
                    "attr": "shoe.size",
                    "op": "eq",
                    "values": ["42"],
                    "source": "stated",
                    "scope": "persistent",
                    "origin": {"kind": "stated"},
                }
            ],
        },
    )
    app.cell.agent_features.suppress(CUSTOMER, "marketing")
    return app


@pytest.fixture
def niadra(store: MockApp) -> Iterator[Niadra]:
    client = _client(store)
    yield client
    client.close()


def _records(app: MockApp) -> list[dict[str, Any]]:
    return [stored.record for stored in app.cell.turns.turns.values()]


def test_the_profile_is_read_once_and_kept(store: MockApp, niadra: Niadra) -> None:
    profile = niadra.profile()
    assert profile is not None
    assert set(profile.features) == {"turns", "signals", "coordination"}
    assert profile.claim_contract is not None and profile.claim_contract.version == RETAIL["version"]
    store.cell.fail_next("/v1/sdk/profile", 503, times=10)
    assert niadra.profile() is profile
    assert store.cell.failures[0].remaining == 10  # still fresh: not asked again


def test_a_space_without_a_profile_is_not_asked_again_for_a_while() -> None:
    app = MockApp()
    app.cell.features.clear()
    client = _client(app)
    assert client.profile() is None
    app.cell.features.add("turns")
    assert client.profile() is None
    assert not client.turns.recording
    client.close()


def test_a_read_with_constraints_records_the_block_it_served(store: MockApp, niadra: Niadra) -> None:
    with niadra.conversation("c-1", subject=CUSTOMER) as conversation, conversation.turn():
        context = conversation.context(include=["constraints"])
    assert context.constraints is not None and context.constraints.version == "cv_0123456789abcdef"
    assert niadra.flush(5)
    (record,) = _records(store)
    read = next(r for r in record["reads"] if r["surface"] == "constraints")
    assert read["version"] == "cv_0123456789abcdef"
    assert record["blobs"][read["blob"]]["content"]["version"] == "cv_0123456789abcdef", "the block it served"


def test_a_block_changes_while_the_pinned_pack_does_not(store: MockApp) -> None:
    store.cell.features.add("state")
    store.cell.agent_features.show(CUSTOMER, {"objects": [], "text": "Pedido 881: separado."})
    niadra = Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="whatsapp",
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=httpx.Client(transport=httpx.WSGITransport(app=store.wsgi)),
        turns=TurnOptions(interval=3600),
    )
    with niadra.conversation("c-3", subject=CUSTOMER) as conversation:
        first = conversation.context(include=["state", "constraints"])
        store.cell.agent_features.show(CUSTOMER, {"objects": [], "text": "Pedido 881: saiu para entrega."})
        second = conversation.context(include=["state", "constraints"])
    assert first.state is not None and first.state.text == "Pedido 881: separado."
    assert second.origin == "cache", "the pack stayed pinned: the server answered not_modified"
    assert second.state is not None and second.state.text == "Pedido 881: saiu para entrega."
    assert second.constraints is not None and second.constraints.version == "cv_0123456789abcdef"
    assert not second.degraded
    niadra.close()


def test_a_block_the_space_does_not_serve_leaves_the_read_whole(store: MockApp, niadra: Niadra) -> None:
    store.cell.features.discard("signals")
    with niadra.conversation("c-2", subject=CUSTOMER) as conversation:
        context = conversation.context(include=["constraints"])
    assert context.text and context.constraints is None and context.origin == "network"
    assert niadra._core.blocks.wanted(["constraints"]) == []


def test_the_claims_of_a_turn_are_counted_against_its_tools(store: MockApp, niadra: Niadra) -> None:
    example = _example()
    with niadra.conversation("c-3", subject=CUSTOMER, agent_id="store") as conversation:
        example.answer(conversation, "Quanto está o PX?", lambda _: "Sai por R$ 199,90, antes R$ 299,90.")
        with conversation.turn():
            conversation.agent("Hoje fica por R$ 149,90.")
            conversation.agent("Esse vestido veste bem quem usa 38 ou 40.")
    assert niadra.flush(5)
    backed, unbacked = sorted(_records(store), key=lambda r: len(r["calls"]), reverse=True)
    assert [(c["role"], c["verdict"], c["action"]) for c in backed["claims"]] == [
        ("price_sale", "matched", "none"),
        ("price_list", "matched", "none"),
    ]
    assert backed["claims"][0]["evidence"] == {
        "call_id": "k1",
        "field": "price_sale",
        "ref": "product:store:PX-4471",
    }
    (claim,) = unbacked["claims"]  # the negative corpus phrase found nothing
    assert (claim["nature"], claim["verdict"], claim["action"]) == ("model", "unsupported", "count")


def test_the_example_agent_keeps_working_with_niadra_down(store: MockApp, niadra: Niadra) -> None:
    example = _example()
    reply = "Sai por R$ 199,90, antes R$ 299,90."
    with niadra.conversation("c-4", subject=CUSTOMER, agent_id="store") as conversation:
        assert example.answer(conversation, "Quanto está o PX?", lambda _: reply) == reply
        assert not example.offer_follow_up(niadra, conversation, CUSTOMER)
        assert niadra.flush(5)
        store.cell.fail_next("/", 503, times=10_000)
        with conversation.turn():
            context = conversation.context(include=["constraints"])
            claims = conversation.claims.check("Fica por R$ 149,90.")
        assert example.answer(conversation, "E o frete?", lambda _: reply) == reply
        assert context.degraded and context.origin == "last_good"
        assert context.constraints is not None and context.constraints.hard[0].values == ["42"]
        assert [(c.verdict, c.action) for c in claims] == [("unsupported", "count")]
        niadra._suppressions._read_at = -1e9  # the copy is old: its refresh fails, and it still holds
        assert not example.offer_follow_up(niadra, conversation, CUSTOMER)
        assert example.offer_follow_up(niadra, conversation, OTHER)
        assert not niadra.flush(1)
        assert niadra.turns.pending >= 1
    store.cell.failures.clear()
    niadra._turn_sender()._pacing.succeeded()
    assert niadra.flush(5)
    assert len(_records(store)) == 3


def test_with_no_copy_and_niadra_down_each_purpose_fails_its_own_way(store: MockApp) -> None:
    store.cell.fail_next("/", 503, times=10_000)
    client = _client(store)
    assert not client.may_contact(OTHER, "marketing")
    assert client.may_contact(OTHER, "service")
    assert client.may_contact(OTHER, "marketing", fail_open=True)
    client.close()


def test_a_space_without_a_suppression_list_suppresses_nothing(store: MockApp) -> None:
    store.cell.features.discard("coordination")
    client = _client(store)
    assert client.may_contact(CUSTOMER, "marketing")
    client.close()


_words = st.sampled_from(
    [
        "Sai",
        "por",
        "de",
        "antes",
        "frete",
        "R$",
        "199,90",
        "299,90",
        "12x",
        "de",
        "10%",
        "dia",
        "20/10",
        "3",
        "o",
    ]
)
_texts = st.lists(
    _words | st.sampled_from(RETAIL["negative_corpus"]["phrases"]), min_size=1, max_size=20
).map(" ".join)


@settings(max_examples=60, deadline=None)
@given(text=_texts, immutable=st.booleans())
def test_count_mode_never_changes_an_output(text: str, immutable: bool) -> None:
    app = MockApp()
    app.cell.agent_features.claim_contract = ClaimContractSummary.model_validate(RETAIL)
    client = _client(app)
    context = "order_confirmation" if immutable else "chat"
    with client.conversation("c-p", subject=CUSTOMER) as conversation, conversation.turn():
        claims = conversation.claims.check(text, context=context, immutable=immutable)
        conversation.agent(text)
    assert client.flush(5)
    sent = [e.text for e in app.cell.events if e.item.speaker.role == "ai_agent"]
    assert sent == [text]
    assert {c.action for c in claims} <= {"none", "count"}
    assert all(0 <= c.span[0] < c.span[1] <= len(text) for c in claims)
    (record,) = [stored.record for stored in app.cell.turns.turns.values()]
    assert {c["action"] for c in record["claims"]} <= {"none", "count"}
    client.close()


async def test_the_async_client_reads_the_profile_and_keeps_the_opt_out(store: MockApp) -> None:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=store.asgi))
    client = AsyncNiadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http)
    profile = await client.profile()
    assert profile is not None and "coordination" in profile.features
    assert not await client.may_contact(CUSTOMER, "marketing")
    store.cell.fail_next("/", 503, times=10_000)
    client._suppressions._read_at = -1e9
    assert not await client.may_contact(CUSTOMER, "marketing")
    assert await client.may_contact(OTHER, "marketing")
    await client.close(timeout=0)
    await http.aclose()
