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
