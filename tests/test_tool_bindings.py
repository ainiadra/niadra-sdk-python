"""The tool bindings the SDK profile serves: a tool without a binding in code measures the constraints block
through the one the space binds for its name, a binding in code wins, and the served capability masks a
tool's output unless the code says otherwise."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from niadra import Niadra, phone
from niadra._profile import ProfileCache
from niadra.errors import NotFoundError
from niadra.options import CacheOptions, TurnOptions
from niadra_mock import MOCK_KEY, MockApp

CUSTOMER = phone("+5511912345678")
SEARCH: dict[str, Any] = {
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
    "capabilities": {"mask_output": True},
}
ITEM: dict[str, Any] = {
    "type": "item_variant",
    "ownership": "shared",
    "mirror_of": {"system": "erp"},
    "fields": {"color": {"type": "text"}, "cost_price": {"type": "money"}},
    "field_access": {"cost_price": "deny"},
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
CARDS = [
    {"variant_id": "1", "color": "blue", "cost_price": 40.0},
    {"variant_id": "2", "color": "green", "cost_price": 50.0},
]


@pytest.fixture
def app() -> MockApp:
    app = MockApp()
    app.cell.features.update({"signals", "state"})
    app.cell.agent_features.declare(ITEM)
    app.cell.agent_features.tool_bindings = [SEARCH]
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


def _applied(app: MockApp, niadra: Niadra, search: Any) -> dict[str, Any]:
    niadra.profile()
    with (
        niadra.conversation("c-1", subject=CUSTOMER, agent_id="stylist") as conversation,
        conversation.turn(),
    ):
        conversation.context(include=["constraints"])
        search(not_color=["red"])
        conversation.agent("Separei duas opções.")
    assert niadra.flush(5)
    (stored,) = app.cell.turns.turns.values()
    return stored.record["calls"][0].get("applied") or {}


def test_a_tool_without_a_binding_in_code_measures_through_the_served_one(
    app: MockApp, niadra: Niadra
) -> None:
    @niadra.tool("search_products")
    def search(not_color: list[str] | None = None) -> dict[str, Any]:
        return {"cards": [dict(c) for c in CARDS]}

    applied = _applied(app, niadra, search)
    assert applied["hard_sent"] == ["h1"] and applied["results_checked"] == 2 and applied["violations"] == 0


def test_a_binding_in_code_wins_over_the_served_one(app: MockApp, niadra: Niadra) -> None:
    own = {
        **SEARCH,
        "args": [{"attr": "item_variant.color", "param": "colour", "negation": {"param": "not_colour"}}],
    }

    @niadra.tool("search_products", binding=own)
    def search(not_color: list[str] | None = None) -> dict[str, Any]:
        return {"cards": [dict(c) for c in CARDS]}

    assert _applied(app, niadra, search)["hard_sent"] == [], "the code's binding names other arguments"


def test_the_served_capability_masks_the_output_unless_the_code_says(app: MockApp, niadra: Niadra) -> None:
    @niadra.tool("search_products")
    def search() -> dict[str, Any]:
        return {"cards": [dict(c) for c in CARDS]}

    @niadra.tool("search_products", mask_output=False)
    def search_plain() -> dict[str, Any]:
        return {"cards": [dict(c) for c in CARDS]}

    niadra.profile()
    assert search() == {
        "cards": [{"variant_id": "1", "color": "blue"}, {"variant_id": "2", "color": "green"}]
    }
    assert search_plain() == {"cards": CARDS}


def test_the_cache_serves_each_tool_by_name_and_forgets_on_a_404() -> None:
    cache = ProfileCache()
    cache.absorb({"features": ["signals"], "tool_bindings": [SEARCH], "valid_for_s": 300})
    served = cache.tool_binding("search_products")
    assert served is not None and served["capabilities"]["mask_output"] is True
    assert served["results"][0]["id"] == "variant_id" and served["capabilities"]["overfetch"] is False
    assert cache.tool_binding("book_visit") is None
    cache.failed(NotFoundError(404))
    assert cache.tool_binding("search_products") is None
