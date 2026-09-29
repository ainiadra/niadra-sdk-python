"""The company's resolvers behind `verify_claim()`, and the content resolver that puts back what Niadra keeps
by pointer (`niadra.resolvers`, `niadra.content`)."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator, Sequence
from typing import Any

import httpx
import pytest

from niadra import AsyncNiadra, Niadra, phone
from niadra.models.state import StateRef
from niadra.options import CacheOptions, TurnOptions
from niadra.resolvers import Resolved
from niadra_mock import MOCK_KEY, MockApp

CUSTOMER = phone("+5511912345678")
QUOTE = "health_quote:op:q-77"


@pytest.fixture
def app() -> MockApp:
    mock = MockApp()
    mock.cell.features.add("state")
    return mock


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


def requote(ref: StateRef, fields: Sequence[str] | None) -> Resolved:
    return Resolved({"price_full": 499.9}, version=7)


def test_a_fresh_value_is_verified_by_niadra(app: MockApp, niadra: Niadra) -> None:
    app.cell.state.observe(QUOTE, {"price_full": 511.06})
    verdict = niadra.verify_claim(QUOTE, "price_full", 511.06)
    assert (verdict.claim_safe, verdict.source, verdict.status) == (True, "niadra", "fresh")


def test_a_stale_value_is_read_again_by_the_resolver_and_the_fresh_one_decides(
    app: MockApp, niadra: Niadra
) -> None:
    app.cell.state.observe(QUOTE, {"price_full": 511.06}, status="stale")
    niadra.resolvers.register("health_quote", requote)
    with niadra.conversation("c-1", subject=CUSTOMER) as conversation, conversation.turn():
        old = conversation.verify_claim(QUOTE, "price_full", 511.06)
        new = conversation.verify_claim(QUOTE, "price_full", "499.90")
    assert (old.claim_safe, old.matches, old.source, old.value) == (False, False, "resolver", 499.9)
    assert (new.claim_safe, new.status) == (True, "fresh")
    assert niadra.flush(5)
    (stored,) = app.cell.turns.turns.values()
    calls = [c for c in stored.record["calls"] if c["name"] == "resolve:health_quote"]
    assert calls and calls[0]["observations"][0]["fields"] == {"price_full": 499.9}


def test_without_a_resolver_a_stale_value_is_never_verified(app: MockApp, niadra: Niadra) -> None:
    app.cell.state.observe(QUOTE, {"price_full": 511.06}, status="stale")
    verdict = niadra.verify_claim(QUOTE, "price_full", 511.06)
    assert (verdict.claim_safe, verdict.source, verdict.status) == (False, "niadra", "stale")


def test_with_niadra_down_the_resolver_still_decides_and_without_it_nothing_is_verified(
    app: MockApp, niadra: Niadra
) -> None:
    app.cell.fail_next("/", 503, times=1000)
    unsure = niadra.verify_claim(QUOTE, "price_full", 499.9)
    assert (unsure.claim_safe, unsure.source, unsure.declared_gaps) == (
        False,
        "none",
        ("source_unreachable",),
    )
    niadra.resolvers.register("health_quote", requote)
    assert niadra.verify_claim(QUOTE, "price_full", 499.9).claim_safe


def test_a_resolver_that_keeps_failing_opens_its_circuit(app: MockApp, niadra: Niadra) -> None:
    calls: list[str] = []

    def broken(ref: StateRef, fields: Sequence[str] | None) -> dict[str, Any]:
        calls.append(ref.id)
        raise RuntimeError("the pricing service is down")

    app.cell.state.observe(QUOTE, {"price_full": 511.06}, status="stale")
    niadra.resolvers.register("health_quote", broken)
    for _ in range(7):
        assert not niadra.verify_claim(QUOTE, "price_full", 511.06).claim_safe
    assert len(calls) == 5 and not niadra.resolvers.available("health_quote")


def test_a_resolver_slower_than_the_budget_verifies_nothing(app: MockApp, niadra: Niadra) -> None:
    def slow(ref: StateRef, fields: Sequence[str] | None) -> dict[str, Any]:
        time.sleep(0.3)
        return {"price_full": 511.06}

    app.cell.state.observe(QUOTE, {"price_full": 511.06}, status="stale")
    niadra.resolvers.register("health_quote", slow)
    started = time.monotonic()
    verdict = niadra.verify_claim(QUOTE, "price_full", 511.06, budget=0.05)
    assert not verdict.claim_safe and time.monotonic() - started < 0.2


async def test_the_async_client_awaits_a_coroutine_resolver(app: MockApp) -> None:
    async def arequote(ref: StateRef, fields: Sequence[str] | None) -> dict[str, Any]:
        return {"price_full": 499.9}

    app.cell.state.observe(QUOTE, {"price_full": 511.06}, status="expired")
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.asgi))
    client = AsyncNiadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http)
    client.resolvers.register("health_quote", arequote)
    verdict = await client.verify_claim(QUOTE, "price_full", 499.9)
    assert (verdict.claim_safe, verdict.source) == (True, "resolver")
    await client.close(timeout=1)
    await http.aclose()


def _content(pointer: str, text: str, scan: str = "clean", digest: str | None = None) -> dict[str, Any]:
    return {
        "claim_safe": False,
        "logic": "yes",
        "status": "fresh",
        "content": {
            "mode": "pointer",
            "pointer": pointer,
            "scan": scan,
            "sha256": digest or hashlib.sha256(text.encode()).hexdigest(),
        },
    }


def test_content_kept_by_pointer_comes_back_in_the_agents_process(app: MockApp, niadra: Niadra) -> None:
    bucket = {
        "p/1": "Petição inicial, fls. 3.",
        "p/2": "Texto com instrução embutida.",
        "p/3": "Outro texto.",
    }
    fetched: list[str] = []

    def read(pointer: str) -> str:
        fetched.append(pointer)
        return bucket[pointer]

    view = {
        "objects": [
            {
                "ref": {"type": "case_file", "namespace": "court", "id": "123"},
                "fields": {
                    "summary": _content("p/1", bucket["p/1"]),
                    "note": _content("p/2", bucket["p/2"], scan="flagged"),
                    "draft": _content("p/3", "another text"),
                },
            }
        ]
    }
    app.cell.agent_features.show(CUSTOMER, view)
    niadra.content.register(read)
    with niadra.conversation("c-2", subject=CUSTOMER) as conversation:
        context = conversation.context(include=["state"])
    assert context.state is not None
    fields = context.state.objects[0].fields
    assert fields["summary"].v == bucket["p/1"]
    assert fields["note"].v is None and "p/2" not in fetched  # flagged content never reaches the model
    assert fields["draft"].v is None  # the text is not the one Niadra recorded
