"""Coordination in the agent's process: the check and its direction when Niadra is down, declarations, claims,
and the contact token checked at the company's gateway."""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from collections.abc import Iterator, MutableMapping
from typing import Any

import httpx
import pytest

from niadra import AsyncNiadra, Niadra, UnprocessableEntityError, phone
from niadra.coordination.token import ContactTokenError
from niadra.options import CacheOptions, Timeouts, TurnOptions
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


def test_the_list_is_read_to_a_short_page_which_still_names_the_cursor_to_go_on_from(app: MockApp) -> None:
    app.cell.agent_features.suppress(CUSTOMER, "service")
    reads: list[str] = []

    def app_with_reads(environ: dict[str, Any], start_response: Any) -> Any:
        if environ["PATH_INFO"] == "/v1/suppressions":
            reads.append(environ.get("QUERY_STRING", ""))
        return app.wsgi(environ, start_response)

    http = httpx.Client(transport=httpx.WSGITransport(app=app_with_reads))
    niadra = Niadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http)
    assert not niadra.may_contact(CUSTOMER, "service")
    assert reads == ["limit=200"]
    niadra.close()
    http.close()


def _slow_list(app: MockApp, seconds: float) -> Any:
    """The mock with each read of the suppression list (its salt, each page) taking `seconds`."""

    def wsgi(environ: dict[str, Any], start_response: Any) -> Any:
        if environ["PATH_INFO"].startswith("/v1/suppressions"):
            time.sleep(seconds)
        return app.wsgi(environ, start_response)

    return wsgi


def _slow_client(app: MockApp, seconds: float) -> tuple[Niadra, httpx.Client]:
    http = httpx.Client(transport=httpx.WSGITransport(app=_slow_list(app, seconds)))
    timeouts = Timeouts(navigation=0.5, connect=0)
    client = Niadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http, timeouts=timeouts)
    return client, http


def test_the_first_check_gives_each_round_trip_of_the_list_its_own_budget(app: MockApp) -> None:
    # 09/10/2026, from Sao Paulo on a new client: the salt and the first page took about 1.1 s together, past
    # the one 0.6 s budget of the whole read, and the first check of a purpose that fails closed said no on
    # every channel, for a customer the list does not name.
    app.cell.agent_features.suppress(CUSTOMER, "marketing")
    niadra, http = _slow_client(app, 0.35)
    assert niadra.may_contact(OTHER, "marketing", channel="voice")
    assert not niadra.may_contact(CUSTOMER, "marketing", channel="voice")
    niadra.close()
    http.close()


def test_a_first_check_that_runs_out_leaves_the_read_going_on_in_the_background(app: MockApp) -> None:
    niadra, http = _slow_client(app, 0.6)
    assert not niadra.may_contact(OTHER, "marketing")  # no copy yet, and marketing waits
    deadline = time.monotonic() + 5
    while not niadra._suppressions.held and time.monotonic() < deadline:
        time.sleep(0.05)
    assert niadra.may_contact(OTHER, "marketing")
    niadra.close()
    http.close()


async def test_the_async_first_check_gives_each_round_trip_of_the_list_its_own_budget(app: MockApp) -> None:
    app.cell.agent_features.suppress(CUSTOMER, "marketing")

    async def asgi(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/v1/suppressions"):
            await asyncio.sleep(0.35)
        await app.asgi(scope, receive, send)

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi))
    timeouts = Timeouts(navigation=0.5, connect=0)
    client = AsyncNiadra(
        MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http, timeouts=timeouts
    )
    assert await client.may_contact(OTHER, "marketing", channel="voice")
    assert not await client.may_contact(CUSTOMER, "marketing", channel="voice")
    await client.close(timeout=1)
    await http.aclose()


def test_a_first_check_beside_a_read_under_way_waits_for_it(app: MockApp) -> None:
    # A first check that found the copy being read by another (a concurrent first check, or the background
    # read an earlier check left) answered at once with no copy: a purpose that fails closed said no for a
    # customer the list does not name, the bug the round trips' own budgets fixed for one check alone.
    reading = threading.Event()

    def wsgi(environ: dict[str, Any], start_response: Any) -> Any:
        if environ["PATH_INFO"].startswith("/v1/suppressions"):
            reading.set()
            time.sleep(0.15)
        return app.wsgi(environ, start_response)

    http = httpx.Client(transport=httpx.WSGITransport(app=wsgi))
    timeouts = Timeouts(navigation=0.5, connect=0)
    niadra = Niadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http, timeouts=timeouts)
    first: list[bool] = []
    other = threading.Thread(target=lambda: first.append(niadra.may_contact(OTHER, "marketing")))
    other.start()
    assert reading.wait(5)
    assert niadra.may_contact(OTHER, "marketing"), "the second check waits for the first one's read"
    other.join(5)
    assert first == [True]
    niadra.close()
    http.close()


def test_a_first_check_waits_for_a_read_under_way_no_longer_than_its_own_round_trips(app: MockApp) -> None:
    gate = threading.Event()

    def wsgi(environ: dict[str, Any], start_response: Any) -> Any:
        if environ["PATH_INFO"].startswith("/v1/suppressions"):
            gate.wait(5)
        return app.wsgi(environ, start_response)

    http = httpx.Client(transport=httpx.WSGITransport(app=wsgi))
    timeouts = Timeouts(navigation=0.1, connect=0, write=5)
    niadra = Niadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http, timeouts=timeouts)
    niadra._read_suppressions_later()  # a background read holds the copy's cursor
    started = time.monotonic()
    assert not niadra.may_contact(OTHER, "marketing")  # no copy yet, and marketing waits
    assert time.monotonic() - started < 1.0, "two round trips of 0.1 s, never the background read's 5 s"
    gate.set()
    niadra.close()
    http.close()


async def test_concurrent_async_first_checks_share_one_read_of_the_list(app: MockApp) -> None:
    salts = 0

    async def asgi(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        nonlocal salts
        if scope["type"] == "http" and scope["path"] == "/v1/suppressions/salt":
            salts += 1
            await asyncio.sleep(0.1)
        await app.asgi(scope, receive, send)

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi))
    timeouts = Timeouts(navigation=0.5, connect=0)
    client = AsyncNiadra(
        MOCK_KEY, base_url="http://mock", channel="whatsapp", http_client=http, timeouts=timeouts
    )
    answers = await asyncio.gather(*(client.may_contact(OTHER, "marketing") for _ in range(3)))
    assert answers == [True, True, True]
    assert salts == 1, "one read from the copy's cursor, never one per check"
    await client.close(timeout=1)
    await http.aclose()


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


@pytest.mark.parametrize(("direction", "decision"), [("outbound", "defer"), ("inbound", "allow")])
def test_a_check_the_api_refuses_is_the_integrations_error_never_unchecked(
    app: MockApp, niadra: Niadra, caplog: pytest.LogCaptureFixture, direction: Any, decision: str
) -> None:
    # A purpose the space does not declare answered 422 and read as `allow`, `unchecked`, as if Niadra were
    # down: an outbound contact now waits, and the log says why.
    app.cell.fail_next("/v1/coordination/check", 422)
    with caplog.at_level(logging.WARNING, logger="niadra"), niadra.conversation("c-9", subject=CUSTOMER) as c:
        result = c.check("deadline_reminder", purpose="legal", direction=direction)
    assert (result.decision, result.reasons, result.valid_for_s) == (decision, ["invalid_request"], 0)
    assert (
        "the coordination check was refused: HTTP 422 invalid_input: injected by niadra-mock (request "
        in caplog.text
    )


def test_strict_raises_a_check_the_api_refuses(app: MockApp) -> None:
    http = httpx.Client(transport=httpx.WSGITransport(app=app.wsgi))
    strict = Niadra(MOCK_KEY, base_url="http://mock", channel="whatsapp", strict=True, http_client=http)
    app.cell.fail_next("/v1/coordination/check", 422)
    with pytest.raises(UnprocessableEntityError), strict.conversation("c-10", subject=CUSTOMER) as c:
        c.check("deadline_reminder", purpose="legal")
    strict.close()
