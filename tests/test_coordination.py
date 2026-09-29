"""Coordination in the agent's process: the check and its direction when Niadra is down, declarations, claims,
and the contact token checked at the company's gateway."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from niadra import AsyncNiadra, Niadra, phone
from niadra.coordination.token import ContactTokenError
from niadra.options import CacheOptions, TurnOptions
from niadra_mock import MOCK_KEY, MockApp
from niadra_mock.coordinate import SPACE

CUSTOMER = phone("+5511912345678")
OTHER = phone("+5511998765432")
GATEWAY_KEY = b"k" * 32


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
    mock.cell.features.add("coordination")
    mock.cell.coordination.gateway("wa_gateway", GATEWAY_KEY)
    return mock


@pytest.fixture
def niadra(app: MockApp) -> Iterator[Niadra]:
    client = _client(app)
    yield client
    client.close()


def _records(app: MockApp) -> list[dict[str, Any]]:
    return [stored.record for stored in app.cell.turns.turns.values()]


def test_a_farewell_goes_once_and_its_effect_is_declared(app: MockApp, niadra: Niadra) -> None:
    key = "farewell:c-1"
    with (
        niadra.conversation("c-1", subject=CUSTOMER, agent_id="closing") as conversation,
        conversation.turn(),
    ):
        first = conversation.check("farewell", purpose="service", effect_key=key)
        assert first.decision == "allow" and first.effect is not None and first.effect.state == "none"
        conversation.declare.effect(key, "done")
        assert niadra.flush(5)
        second = conversation.check("farewell", purpose="service", effect_key=key)
    assert (second.decision, second.reasons) == ("deny", ["effect_done"])
    (declared,) = app.cell.coordination.declarations
    assert declared["kind"] == "effect" and declared["detail"] == {
        "effect_key": key,
        "state": "done",
        "attempt": 1,
    }
    assert niadra.flush(5)
    (record,) = _records(app)
    assert [c["decision"] for c in record["coordination"]] == ["allow", "deny"]
    assert record["effects"] == [{"key": key, "state": "done"}]


@pytest.mark.parametrize(
    ("purpose", "direction", "effect_key", "decision", "reason"),
    [
        ("marketing", "outbound", None, "defer", "unavailable"),
        ("collection", "outbound", None, "defer", "unavailable"),
        ("service", "outbound", None, "allow", "unchecked"),
        ("transactional", "outbound", None, "allow", "unchecked"),
        ("marketing", "inbound", None, "allow", "unchecked"),
        ("service", "outbound", "farewell:c-2", "defer", "unavailable"),
    ],
)
def test_with_niadra_down_each_purpose_fails_its_own_way(
    app: MockApp,
    niadra: Niadra,
    purpose: str,
    direction: Any,
    effect_key: str | None,
    decision: str,
    reason: str,
) -> None:
    app.cell.fail_next("/", 503, times=100)
    with niadra.conversation("c-2", subject=CUSTOMER) as conversation:
        result = conversation.check("offer", purpose=purpose, direction=direction, effect_key=effect_key)
    assert (result.decision, result.reasons, result.contact_token) == (decision, [reason], None)


def test_with_niadra_down_the_local_opt_out_still_denies(app: MockApp, niadra: Niadra) -> None:
    app.cell.agent_features.suppress(CUSTOMER, "service")
    assert not niadra.may_contact(CUSTOMER, "service")  # the copy is read while Niadra answers
    app.cell.fail_next("/", 503, times=100)
    with niadra.conversation("c-3", subject=CUSTOMER) as conversation:
        result = conversation.check("follow_up", purpose="service")
    assert (result.decision, result.reasons) == ("deny", ["suppressed"])


def test_a_space_that_does_not_coordinate_holds_nothing_back(app: MockApp, niadra: Niadra) -> None:
    app.cell.features.discard("coordination")
    with niadra.conversation("c-4", subject=CUSTOMER) as conversation:
        result = conversation.check("offer", purpose="marketing")
    assert (result.decision, result.reasons) == ("allow", ["unchecked"])


def test_declarations_wait_for_niadra_and_leave_when_it_answers(app: MockApp, niadra: Niadra) -> None:
    app.cell.fail_next("/v1/coordination/declare", 503, times=1000)
    with niadra.conversation("c-5", subject=CUSTOMER, agent_id="closing") as conversation:
        conversation.declare.effect("farewell:c-5", "unknown_outcome", attempt=1)
        assert not niadra._outbox.flush(0.5)
    assert len(niadra._outbox) == 1 and not app.cell.coordination.declarations
    app.cell.failures.clear()
    niadra._outbox._pacing.succeeded()
    assert niadra._outbox.flush(5)
    (declared,) = app.cell.coordination.declarations
    assert declared["detail"]["state"] == "unknown_outcome"


def test_a_task_lock_is_held_by_one_agent_at_a_time(app: MockApp, niadra: Niadra) -> None:
    hearing = "hearing:tj:123"
    with niadra.task("t-1", agent_id="summarizer") as first, niadra.task("t-2", agent_id="drafter") as second:
        mine = first.claim(object=hearing, task="hearing_summary", lease_s=1500)
        theirs = second.claim(object=hearing, task="hearing_summary", lease_s=1500)
    assert mine.held and mine.claim is not None and mine.claim.kind == "task_lock"
    assert (theirs.held, theirs.error) == (False, "task_locked")


def test_a_marketing_token_passes_the_gateway_once(app: MockApp, niadra: Niadra) -> None:
    app.cell.coordination.budget("marketing", 1)
    key = base64.urlsafe_b64encode(GATEWAY_KEY).rstrip(b"=").decode()
    gateway = niadra.contact_gateway("wa_gateway", space=SPACE, key=key)
    with niadra.conversation("c-6", subject=CUSTOMER, agent_id="retention") as conversation:
        decision = conversation.check("winback", purpose="marketing", gateway_id="wa_gateway")
        again = conversation.check("winback", purpose="marketing", gateway_id="wa_gateway")
    assert decision.decision == "allow" and decision.contact_token is not None
    assert (again.decision, again.reasons, again.contact_token) == ("deny", ["budget_exhausted"], None)
    claims = gateway.verify(decision.contact_token, handle=CUSTOMER, channel="whatsapp")
    assert (claims.purpose, claims.gateway) == ("marketing", "wa_gateway")
    with pytest.raises(ContactTokenError) as replayed:
        gateway.verify(decision.contact_token, handle=CUSTOMER, channel="whatsapp")
    assert replayed.value.code == "replayed"


def test_a_token_for_another_destination_is_refused(app: MockApp, niadra: Niadra) -> None:
    key = base64.urlsafe_b64encode(GATEWAY_KEY).rstrip(b"=").decode()
    gateway = niadra.contact_gateway("wa_gateway", space=SPACE, key=key)
    with niadra.conversation("c-7", subject=CUSTOMER) as conversation:
        decision = conversation.check("winback", purpose="marketing", gateway_id="wa_gateway")
    assert decision.contact_token is not None
    with pytest.raises(ContactTokenError) as refused:
        gateway.verify(decision.contact_token, handle=OTHER, channel="whatsapp")
    assert refused.value.code == "wrong_recipient"


def test_the_gateway_keeps_checking_with_the_last_keys_while_niadra_is_down(
    app: MockApp, niadra: Niadra
) -> None:
    key = base64.urlsafe_b64encode(GATEWAY_KEY).rstrip(b"=").decode()
    gateway = niadra.contact_gateway("wa_gateway", space=SPACE, key=key)
    assert gateway.refresh() and gateway.keys
    with niadra.conversation("c-8", subject=CUSTOMER) as conversation:
        decision = conversation.check("winback", purpose="marketing", gateway_id="wa_gateway")
    assert decision.contact_token is not None
    app.cell.fail_next("/", 503, times=100)
    assert not gateway.refresh()
    assert gateway.verify(decision.contact_token, handle=CUSTOMER, channel="whatsapp").purpose == "marketing"


async def test_the_async_client_checks_declares_and_verifies(app: MockApp) -> None:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.asgi))
    client = AsyncNiadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http)
    key = base64.urlsafe_b64encode(GATEWAY_KEY).rstrip(b"=").decode()
    gateway = client.contact_gateway("wa_gateway", space=SPACE, key=key)
    async with client.conversation("c-9", subject=CUSTOMER, agent_id="retention") as conversation:
        decision = await conversation.check("winback", purpose="marketing", gateway_id="wa_gateway")
        assert decision.contact_token is not None
        claims = await gateway.verify(decision.contact_token, handle=CUSTOMER, channel="whatsapp")
        conversation.declare.contact_made(
            decision, purpose="marketing", channel="whatsapp", gateway_id="wa_gateway", jti=claims.jti
        )
        assert await client.flush(5)
    (declared,) = app.cell.coordination.declarations
    assert declared["detail"]["jti"] == claims.jti and declared["detail"]["unchecked"] is False
    await client.close(timeout=1)
    await http.aclose()
