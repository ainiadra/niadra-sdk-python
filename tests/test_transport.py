from __future__ import annotations

import httpx
import pytest
import respx

from niadra import (
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
    ServerError,
    UnprocessableEntityError,
)
from niadra._transport import Request, SyncTransport
from tests.conftest import BASE, KEY

PROBLEM = {
    "type": "https://docs.niadra.com/errors/invalid_input",
    "title": "invalid input",
    "status": 422,
    "code": "invalid_input",
    "request_id": "req-1",
}


@pytest.fixture
def transport() -> SyncTransport:
    return SyncTransport(BASE, KEY)


def post(budget: float | None = None) -> Request:
    return Request(
        "POST", "/v1/batch", json={"items": []}, timeout=1.0, budget=budget, idempotency_key="idem-1"
    )


def test_sends_auth_user_agent_and_idempotency_headers(
    respx_mock: respx.MockRouter, transport: SyncTransport
) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").respond(200, json={"ok": True})
    assert transport.request(post()) == {"ok": True}
    headers = route.calls.last.request.headers
    assert headers["authorization"] == f"Bearer {KEY}"
    assert headers["idempotency-key"] == "idem-1"
    assert headers["user-agent"].startswith("niadra-python/")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_retries_server_errors_up_to_three_attempts(
    respx_mock: respx.MockRouter, transport: SyncTransport, status: int
) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").mock(
        side_effect=[httpx.Response(status), httpx.Response(status), httpx.Response(200, json={})]
    )
    transport.request(post())
    assert route.call_count == 3


def test_gives_up_after_three_attempts(respx_mock: respx.MockRouter, transport: SyncTransport) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").respond(503)
    with pytest.raises(ServerError):
        transport.request(post())
    assert route.call_count == 3


def test_same_idempotency_key_on_every_attempt(
    respx_mock: respx.MockRouter, transport: SyncTransport
) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={})]
    )
    transport.request(post())
    assert {call.request.headers["idempotency-key"] for call in route.calls} == {"idem-1"}


@pytest.mark.parametrize(
    ("status", "error"), [(400, BadRequestError), (401, AuthenticationError), (422, UnprocessableEntityError)]
)
def test_never_retries_other_client_errors(
    respx_mock: respx.MockRouter, transport: SyncTransport, status: int, error: type[Exception]
) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").respond(status, json={**PROBLEM, "status": status})
    with pytest.raises(error) as caught:
        transport.request(post())
    assert route.call_count == 1
    assert caught.value.code == "invalid_input"  # type: ignore[attr-defined]
    assert caught.value.request_id == "req-1"  # type: ignore[attr-defined]


def test_retries_429_after_retry_after(respx_mock: respx.MockRouter, transport: SyncTransport) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json={})]
    )
    transport.request(post())
    assert route.call_count == 2


def test_429_that_does_not_fit_the_budget_is_final(
    respx_mock: respx.MockRouter, transport: SyncTransport
) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").respond(429, headers={"Retry-After": "30"})
    with pytest.raises(RateLimitError) as caught:
        transport.request(post(budget=0.5))
    assert caught.value.retry_after == 30
    assert route.call_count == 1


def test_421_retries_at_once_on_a_fresh_connection_pool(
    respx_mock: respx.MockRouter, transport: SyncTransport
) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").mock(
        side_effect=[httpx.Response(421), httpx.Response(200, json={})]
    )
    first_pool = transport._client
    transport.request(post(budget=0.3))
    assert route.call_count == 2
    assert transport._client is not first_pool
    assert first_pool in transport._retired
    transport.close()
    assert first_pool.is_closed


def test_421_keeps_a_caller_supplied_client(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/batch").mock(side_effect=[httpx.Response(421), httpx.Response(200, json={})])
    own = httpx.Client()
    transport = SyncTransport(BASE, KEY, own)
    transport.request(post())
    assert transport._client is own
    transport.close()
    assert not own.is_closed
    own.close()


def test_network_errors_are_retried(respx_mock: respx.MockRouter, transport: SyncTransport) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={})]
    )
    transport.request(post())
    assert route.call_count == 2


def test_a_spent_budget_raises_a_timeout(respx_mock: respx.MockRouter, transport: SyncTransport) -> None:
    respx_mock.post(f"{BASE}/v1/batch").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(APITimeoutError):
        transport.request(post(budget=0.05))


async def test_async_transport_follows_the_same_rules(respx_mock: respx.MockRouter) -> None:
    from niadra._transport import AsyncTransport

    route = respx_mock.post(f"{BASE}/v1/batch").mock(
        side_effect=[httpx.Response(421), httpx.Response(503), httpx.Response(200, json={"ok": 1})]
    )
    transport = AsyncTransport(BASE, KEY)
    assert await transport.request(post()) == {"ok": 1}
    assert route.call_count == 3
    await transport.aclose()


DEPRECATED = {
    "deprecation": "@1790812800",
    "sunset": "Sat, 02 Oct 2027 00:00:00 GMT",
    "link": '<https://docs.niadra.com/en/changelog#old-route>; rel="deprecation"',
}


@pytest.fixture
def fresh_deprecations(monkeypatch: pytest.MonkeyPatch) -> None:
    from niadra import _transport

    monkeypatch.setattr(_transport, "_deprecations_seen", set())


@pytest.mark.usefixtures("fresh_deprecations")
def test_a_deprecated_route_warns_once_per_process_with_its_dates_and_link(
    respx_mock: respx.MockRouter, transport: SyncTransport, caplog: pytest.LogCaptureFixture
) -> None:
    respx_mock.get(url__regex=rf"{BASE}/v1/old/.*").respond(200, json={}, headers=DEPRECATED)
    respx_mock.get(f"{BASE}/v1/new").respond(200, json={})
    with caplog.at_level("WARNING", logger="niadra"):
        for item in ("a-customer-id", "another-id"):
            transport.request(Request("GET", f"/v1/old/{item}"))
        transport.request(Request("GET", "/v1/new"))
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        "niadra: the API deprecated a GET route this client calls, since 2026-10-01; it stops answering"
        " on 2027-10-02. See https://docs.niadra.com/en/changelog#old-route"
    ]
    assert "customer-id" not in warnings[0]


@pytest.mark.usefixtures("fresh_deprecations")
def test_each_deprecated_route_warns_on_its_own_even_on_an_error(
    respx_mock: respx.MockRouter, transport: SyncTransport, caplog: pytest.LogCaptureFixture
) -> None:
    other = {**DEPRECATED, "link": '<https://docs.niadra.com/en/changelog#other>; rel="deprecation"'}
    respx_mock.get(f"{BASE}/v1/old").respond(200, json={}, headers=DEPRECATED)
    respx_mock.post(f"{BASE}/v1/other").respond(422, json=PROBLEM, headers=other)
    with caplog.at_level("WARNING", logger="niadra"):
        transport.request(Request("GET", "/v1/old"))
        with pytest.raises(UnprocessableEntityError):
            transport.request(Request("POST", "/v1/other", json={}))
    links = [r.getMessage().rsplit(" ", 1)[-1] for r in caplog.records if r.levelname == "WARNING"]
    assert links == [
        "https://docs.niadra.com/en/changelog#old-route",
        "https://docs.niadra.com/en/changelog#other",
    ]


@pytest.mark.usefixtures("fresh_deprecations")
async def test_the_async_transport_warns_too(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    from niadra._transport import AsyncTransport

    respx_mock.get(f"{BASE}/v1/old").respond(200, json={}, headers={"deprecation": "@1790812800"})
    transport = AsyncTransport(BASE, KEY)
    with caplog.at_level("WARNING", logger="niadra"):
        await transport.request(Request("GET", "/v1/old"))
        await transport.request(Request("GET", "/v1/old"))
    await transport.aclose()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        "niadra: the API deprecated a GET route this client calls, since 2026-10-01; it stops answering"
        " on a date not announced yet. See https://docs.niadra.com/en/security/api-versioning"
    ]


def test_an_api_error_says_its_code_detail_and_request_id(
    respx_mock: respx.MockRouter, transport: SyncTransport
) -> None:
    # A traceback or `str(error)` carries what the API said, never only the status and code.
    problem = {
        "code": "invalid_input",
        "detail": "unknown purpose legal",
        "request_id": "req-7",
        "status": 422,
    }
    respx_mock.post(f"{BASE}/v1/batch").mock(return_value=httpx.Response(422, json=problem))
    with pytest.raises(UnprocessableEntityError) as raised:
        transport.request(post())
    assert str(raised.value) == "HTTP 422 invalid_input: unknown purpose legal (request req-7)"
