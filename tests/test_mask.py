"""Field access at the tool's output: a field the key may not read never reaches the model, by the SDK
profile's `field_access`, the last one read when Niadra is down, and fail closed only when asked."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from niadra import Niadra, phone
from niadra.options import CacheOptions, TurnOptions
from niadra.turns.mask import MASKED, WITHHELD
from niadra_mock import MOCK_KEY, MockApp

ITEM: dict[str, Any] = {
    "type": "item_variant",
    "ownership": "shared",
    "mirror_of": {"system": "erp"},
    "fields": {
        "price_sale": {"type": "money"},
        "cost_price": {"type": "money"},
        "margin": {"type": "percent"},
    },
    "field_access": {"cost_price": "deny", "margin": "mask"},
}
CARDS: dict[str, Any] = {
    "cards": [{"variant_id": "991", "price_sale": 199.9, "cost_price": 80.0, "margin": 0.6}],
    "meta": {"cost_price": 1},
}


def _shown(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"ref": f"item_variant:store:{c['variant_id']}", "fields": {"price_sale": c["price_sale"]}}
        for c in result["cards"]
    ]


@pytest.fixture
def app() -> MockApp:
    app = MockApp()
    app.cell.features.add("state")
    app.cell.agent_features.declare(ITEM)
    return app


@pytest.fixture
def niadra(app: MockApp) -> Iterator[Niadra]:
    client = Niadra(
        MOCK_KEY,
        base_url="http://mock",
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=httpx.Client(transport=httpx.WSGITransport(app=app.wsgi)),
        turns=TurnOptions(interval=3600),
    )
    yield client
    client.close()


def test_the_model_never_gets_a_denied_or_masked_value(app: MockApp, niadra: Niadra) -> None:
    @niadra.tool("search", provenance=_shown, mask_output=True)
    def search() -> dict[str, Any]:
        return {"cards": [dict(c) for c in CARDS["cards"]], "meta": dict(CARDS["meta"])}

    niadra.profile()
    expected = {"cards": [{"variant_id": "991", "price_sale": 199.9, "margin": MASKED}], "meta": {}}
    assert search() == expected, "outside a turn too"
    with niadra.conversation("c-1", subject=phone("+5511912345678")) as conversation, conversation.turn():
        assert search() == expected
    assert niadra.flush(5)
    (stored,) = app.cell.turns.turns.values()
    blob = stored.record["blobs"][stored.record["calls"][0]["result_model"]]["content"]
    assert blob == expected, "the record holds what the model saw"


def test_niadra_down_keeps_the_last_profile_and_none_ever_read_passes_unless_blocked(
    app: MockApp, niadra: Niadra
) -> None:
    @niadra.tool("search", provenance=_shown, mask_output=True)
    def search() -> dict[str, Any]:
        return {"cards": [dict(c) for c in CARDS["cards"]]}

    @niadra.tool("strict_search", provenance=_shown, mask_output=True, on_unknown="block")
    def strict_search() -> dict[str, Any] | str:
        return {"cards": [dict(c) for c in CARDS["cards"]]}

    assert search()["cards"][0]["cost_price"] == 80.0, "no profile read yet: it passes"
    assert strict_search() == WITHHELD, "unless the tool fails closed"
    niadra.profile()
    app.cell.fail_next("/v1/sdk/profile", 503, times=10)
    niadra._profile._fetched_at = -1e9  # the profile is due again, and Niadra does not answer
    niadra.profile()
    assert "cost_price" not in search()["cards"][0]


def test_a_tool_without_provenance_hides_every_declared_types_fields(app: MockApp, niadra: Niadra) -> None:
    @niadra.tool("lookup", mask_output=True)
    def lookup() -> str:
        return '{"cost_price": 80, "name": "Vestido"}'

    niadra.profile()
    assert lookup() == '{"name": "Vestido"}'
