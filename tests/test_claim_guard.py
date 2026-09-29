"""The claim guard that acts, on a whole output and in a stream (`niadra.turns.guard`)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from niadra import Niadra, phone
from niadra.models.state import ClaimContractSummary
from niadra.options import CacheOptions, TurnOptions
from niadra.turns.capture import StateValue, TurnFrame
from niadra.turns.guard import Guard, guard_async_stream, guard_stream, guard_text
from niadra_mock import MOCK_KEY, MockApp

ROOT = Path(__file__).resolve().parents[1]
HEALTH = json.loads((ROOT / "spec" / "examples" / "claim-contract" / "health-plan-sales.json").read_text())
CONTRACT = ClaimContractSummary.model_validate(HEALTH)
RETAIL = ClaimContractSummary.model_validate(
    json.loads((ROOT / "spec" / "examples" / "claim-contract" / "retail.json").read_text())
)
QUOTE = "health_quote:op:q-77"
CAVEAT = "Não consigo cotar plano empresarial por aqui; já te passo para quem cota."


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _frame(agent: str = "sales") -> TurnFrame:
    """A turn that read a quote as stale for claims, then quoted it again with the tool."""
    frame = TurnFrame(None, agent=agent, conversation_id="c-1")
    frame.observe_state([StateValue(QUOTE, "price_full", 511.06, claim_safe=False, role="price_full")])
    with frame.tool_call("quote", {"plan": "ouro"}) as call:
        call.result({"id": "q-77"}, observations=[{"ref": QUOTE, "fields": {"price_full": 499.9}}])
    return frame


def test_a_stale_copy_of_one_field_is_rewritten_to_its_fresh_value() -> None:
    frame = _frame()
    guarded = guard_text(CONTRACT, frame, "O plano ouro sai por R$ 511,06 no valor cheio. Fechamos?")
    assert guarded.text == "O plano ouro sai por R$ 499,90 no valor cheio. Fechamos?"
    (claim,) = guarded.claims
    assert (claim.verdict, claim.action, claim.role) == ("stale", "rewrite", "price_full")
    assert "guard_acted" in frame.flags
    assert frame.claims == [
        c.model_dump(mode="json", by_alias=True, exclude_none=True) for c in guarded.claims
    ]


def test_a_rewrite_that_is_not_unequivocal_is_a_warning() -> None:
    # Two amounts of the class in the sentence: one could be derived from the other.
    text = "Sai por R$ 511,06 no valor cheio, R$ 1.022,12 para dois."
    guarded = guard_text(CONTRACT, _frame(), text)
    assert guarded.text == text
    assert guarded.claims[0].action == "warn"


def test_a_blocked_claim_gives_its_sentence_to_the_caveat_once() -> None:
    frame = TurnFrame(None, agent="sales", conversation_id="c-1")
    text = "Para PME fica R$ 1.200,00 por vida. No MEI sai R$ 900,00. Posso ajudar em algo mais?"
    guarded = guard_text(CONTRACT, frame, text)
    assert guarded.text == f"{CAVEAT} Posso ajudar em algo mais?"
    assert {(c.category, c.action) for c in guarded.claims} == {
        ("business_plan_price", "block"),
        ("price", "block"),
    }
    assert guarded.changed and not guarded.review


def test_an_immutable_output_never_changes_and_a_block_sends_it_to_a_person() -> None:
    frame = _frame()
    text = "Proposta: plano ouro, R$ 511,06 no valor cheio; PME a R$ 1.200,00 por vida."
    guarded = guard_text(CONTRACT, frame, text, context="proposal")
    assert guarded.text == text
    assert guarded.review and not guarded.changed
    assert "rewrite" not in {c.action for c in guarded.claims}


def test_a_stream_is_rewritten_while_it_flows() -> None:
    frame = TurnFrame(None, agent="store", conversation_id="c-1")
    frame.observe_state(
        [StateValue("product:store:PX", "price_sale", 199.9, claim_safe=False, role="price_sale")]
    )
    with frame.tool_call("check_price", {"sku": "PX"}) as call:
        call.result({}, observations=[{"ref": "product:store:PX", "fields": {"price_sale": 149.9}}])
    chunks = ["O vestido sai por R$ 1", "99,90 hoje", ". Qu", "er levar?"]
    guard = Guard(RETAIL, frame, hold=60, message=60)
    pieces = list(guard_stream(guard, chunks))
    assert "".join(pieces) == "O vestido sai por R$ 149,90 hoje. Quer levar?"
    assert pieces[0] == "O vestido sai por "  # what could start no claim went at once
    assert guard.result is not None and guard.result.claims[0].action == "rewrite"


def test_where_a_context_blocks_the_text_goes_sentence_by_sentence() -> None:
    guard = Guard(CONTRACT, _frame(), hold=60, message=60)
    assert guard.feed("O plano ouro é ótimo. O valor ") == "O plano ouro é ótimo."
    assert guard.feed("cheio sai R$ 499,90. E") == " O valor cheio sai R$ 499,90."


def test_a_hold_past_its_budget_lets_the_text_go_as_it_is() -> None:
    clock = Clock()
    frame = _frame()
    guard = Guard(CONTRACT, frame, clock=clock)
    assert guard.feed("O plano ouro sai por R$ 511,06 no valor ") == ""
    clock.now = 0.2
    released = guard.feed("cheio. Fechamos?")
    tail = guard.finish()
    assert released + tail == "O plano ouro sai por R$ 511,06 no valor cheio. Fechamos?"
    assert guard.result is not None
    assert [(c.verdict, c.action) for c in guard.result.claims] == [("stale", "warn")]
    assert {"guard_budget_exceeded", "guard_acted"} <= frame.flags


async def test_an_async_hold_runs_out_without_waiting_for_the_next_chunk() -> None:
    release = asyncio.Event()

    async def stream() -> AsyncIterator[str]:
        yield "Sai por R$ 511,06 no valor "
        await release.wait()
        yield "cheio."

    guard = Guard(CONTRACT, _frame(), hold=0.02)
    seen: list[str] = []
    async for piece in guard_async_stream(guard, stream()):
        seen.append(piece)
        release.set()
    assert seen[0] == "Sai por R$ 511,06 no valor "
    assert "".join(seen) == "Sai por R$ 511,06 no valor cheio."


def test_without_a_candidate_nothing_is_held() -> None:
    guard = Guard(RETAIL, TurnFrame(None, agent="store", conversation_id="c-1"))
    assert guard.feed("Oi, tudo bem? Posso ") == "Oi, tudo bem? Posso "
    assert guard.feed("ajudar") == ""  # a word that may still grow
    assert guard.finish() == "ajudar"


def _client(app: MockApp) -> Niadra:
    return Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="whatsapp",
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=httpx.Client(transport=httpx.WSGITransport(app=app.wsgi)),
        turns=TurnOptions(interval=3600),
    )


@pytest.fixture
def app() -> MockApp:
    mock = MockApp()
    mock.cell.agent_features.claim_contract = CONTRACT
    return mock


def test_the_record_carries_the_act_the_guard_took_once(app: MockApp) -> None:
    client = _client(app)
    chunks = ["Para PME fica R$ 1.200,00 ", "por vida. Algo mais?"]
    with (
        client.conversation("c-9", subject=phone("+5511912345678"), agent_id="sales") as conversation,
        conversation.turn(),
    ):
        reply = "".join(conversation.claims.guard(iter(chunks)))
        conversation.agent(reply)
    assert client.flush(5)
    (stored,) = app.cell.turns.turns.values()
    record: dict[str, Any] = stored.record
    assert reply == f"{CAVEAT} Algo mais?"
    assert sorted(c["action"] for c in record["claims"]) == ["block", "block"]
    assert "guard_acted" in record["flags"]
    client.close()


_pieces = st.sampled_from(
    [
        "Sai por ",
        "R$ 511,06",
        " no valor cheio",
        ". ",
        "PME ",
        "R$ 1.200,00",
        " por vida",
        "carência de 30 dias",
        "\n",
    ]
)


@settings(max_examples=80, deadline=None)
@given(chunks=st.lists(_pieces, min_size=1, max_size=12))
def test_an_immutable_output_is_never_altered(chunks: list[str]) -> None:
    text = "".join(chunks)
    guard = Guard(CONTRACT, _frame(), context="proposal", hold=60, message=60)
    assert "".join(guard_stream(guard, chunks)) == text
    assert guard.result is not None and guard.result.text == text


@settings(max_examples=80, deadline=None)
@given(chunks=st.lists(_pieces, min_size=1, max_size=12))
def test_a_mutable_stream_ends_as_the_whole_text_would(chunks: list[str]) -> None:
    whole = guard_text(CONTRACT, _frame(), "".join(chunks)).text
    streamed = "".join(guard_stream(Guard(CONTRACT, _frame(), hold=60, message=60), chunks))
    assert streamed == whole


def _chunks(text: str, size: int) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i : i + size]
