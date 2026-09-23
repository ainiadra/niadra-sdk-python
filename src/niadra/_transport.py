"""HTTP transport shared by the sync and async clients.

Retry and budget decisions live in `RetryState`, which does no I/O. `SyncTransport` and
`AsyncTransport` are the two thin loops that drive it, so both clients follow exactly the
same rules without one wrapping the other.

The rules:

- Up to `max_attempts` attempts (3 by default).
- 421 means the space is moving to another cell: drop pooled connections, so the retry
  resolves the address again, and retry at once.
- 429 waits for `Retry-After` when it fits the budget; 500, 502, 503, 504 and network
  errors back off exponentially with jitter.
- Any other 4xx is final.
- Reads carry a total budget (`deadline`): no attempt starts, and no backoff sleeps, past it.

Media bytes go to a pre-signed storage URL through `upload()`, under the same retry rules but
without the source key: the URL itself is the credential, and the key must never leave for a
host other than the API. The upload carries exactly the headers the reservation named, which
the signature covers (content type, and the digest the store checks the body against).
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from niadra._version import __version__
from niadra.errors import APIConnectionError, APIError, APITimeoutError, error_for_status
from niadra.models.common import Problem

RETRY_STATUSES = frozenset({421, 429, 500, 502, 503, 504})
USER_AGENT = f"niadra-python/{__version__}"


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    json: Any = None
    params: dict[str, str] | None = None
    timeout: float = 5.0
    budget: float | None = None
    idempotency_key: str | None = None
    max_attempts: int = 3


@dataclass
class Decision:
    retry: bool
    delay: float = 0.0
    reconnect: bool = False
    error: Exception | None = None


@dataclass
class RetryState:
    request: Request
    base_delay: float = 0.2
    max_delay: float = 8.0
    attempt: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def deadline(self) -> float | None:
        return None if self.request.budget is None else self.started + self.request.budget

    def attempt_timeout(self) -> float | None:
        """The timeout for the next attempt, or None when the budget is spent."""
        self.attempt += 1
        deadline = self.deadline
        if deadline is None:
            return self.request.timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0.001:
            return None
        return min(self.request.timeout, remaining)

    def on_response(self, status: int, headers: httpx.Headers, body: bytes) -> Decision | None:
        """None means success; otherwise whether and when to try again."""
        if status < 400:
            return None
        retry_after = _retry_after(headers.get("retry-after"))
        error = error_for_status(status, _problem(body), retry_after)
        if status not in RETRY_STATUSES:
            return Decision(retry=False, error=error)
        if status == 421:
            return self._next(0.0, error, reconnect=True)
        delay: float = retry_after if status == 429 and retry_after is not None else self._backoff()
        return self._next(delay, error)

    def on_exception(self, exc: httpx.HTTPError) -> Decision:
        if isinstance(exc, httpx.TimeoutException):
            error: Exception = APITimeoutError(f"{self.request.method} {self.request.path} timed out")
        else:
            error = APIConnectionError(f"{self.request.method} {self.request.path}: {type(exc).__name__}")
        return self._next(self._backoff(), error)

    def budget_error(self) -> APITimeoutError:
        return APITimeoutError(f"{self.request.method} {self.request.path} ran out of its time budget")

    def _next(self, delay: float, error: Exception, *, reconnect: bool = False) -> Decision:
        if self.attempt >= self.request.max_attempts:
            return Decision(retry=False, error=error)
        deadline = self.deadline
        if deadline is not None and time.monotonic() + delay >= deadline:
            return Decision(retry=False, error=error)
        return Decision(retry=True, delay=delay, reconnect=reconnect, error=error)

    def _backoff(self) -> float:
        ceiling = min(self.max_delay, self.base_delay * 2 ** (self.attempt - 1))
        return float(ceiling * (0.5 + random.random() / 2))


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # the HTTP-date form is rare for this API; fall back to backoff


def _problem(body: bytes) -> Problem | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Problem.model_validate(data)
    except ValueError:
        return None


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise APIError(response.status_code, None) from exc


def _upload_request(timeout: float) -> Request:
    # The URL carries a signature, so errors and logs name the step, never the URL.
    return Request("PUT", "(media upload URL)", timeout=timeout)


def _headers(api_key: str, request: Request) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if request.idempotency_key:
        headers["Idempotency-Key"] = request.idempotency_key
    return headers


class SyncTransport:
    def __init__(self, base_url: str, api_key: str, http_client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client()
        self._retired: list[httpx.Client] = []
        self._lock = threading.Lock()

    def request(self, request: Request) -> Any:
        state = RetryState(request)
        while True:
            timeout = state.attempt_timeout()
            if timeout is None:
                raise state.budget_error()
            try:
                response = self._client.request(
                    request.method,
                    self._base_url + request.path,
                    json=request.json,
                    params=request.params,
                    headers=_headers(self._api_key, request),
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                decision = state.on_exception(exc)
            else:
                outcome = state.on_response(response.status_code, response.headers, response.content)
                if outcome is None:
                    return _decode(response)
                decision = outcome
            if not decision.retry:
                assert decision.error is not None
                raise decision.error
            if decision.reconnect:
                self._reconnect()
            if decision.delay:
                time.sleep(decision.delay)

    def upload(self, url: str, data: bytes, headers: dict[str, str], timeout: float) -> None:
        state = RetryState(_upload_request(timeout))
        while True:
            attempt_timeout = state.attempt_timeout()
            if attempt_timeout is None:
                raise state.budget_error()
            try:
                response = self._client.put(url, content=data, headers=headers, timeout=attempt_timeout)
            except httpx.HTTPError as exc:
                decision = state.on_exception(exc)
            else:
                outcome = state.on_response(response.status_code, response.headers, response.content)
                if outcome is None:
                    return
                decision = outcome
            if not decision.retry:
                assert decision.error is not None
                raise decision.error
            if decision.delay:
                time.sleep(decision.delay)

    def _reconnect(self) -> None:
        # Other threads may still be mid-request on the old pool, so it is retired, not closed.
        if not self._owns_client:
            return
        with self._lock:
            self._retired.append(self._client)
            self._client = httpx.Client()

    def close(self) -> None:
        if not self._owns_client:
            return
        with self._lock:
            clients, self._retired = [*self._retired, self._client], []
        for client in clients:
            client.close()


class AsyncTransport:
    def __init__(self, base_url: str, api_key: str, http_client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient()
        self._retired: list[httpx.AsyncClient] = []

    async def request(self, request: Request) -> Any:
        state = RetryState(request)
        while True:
            timeout = state.attempt_timeout()
            if timeout is None:
                raise state.budget_error()
            try:
                response = await self._client.request(
                    request.method,
                    self._base_url + request.path,
                    json=request.json,
                    params=request.params,
                    headers=_headers(self._api_key, request),
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                decision = state.on_exception(exc)
            else:
                outcome = state.on_response(response.status_code, response.headers, response.content)
                if outcome is None:
                    return _decode(response)
                decision = outcome
            if not decision.retry:
                assert decision.error is not None
                raise decision.error
            if decision.reconnect:
                await self._reconnect()
            if decision.delay:
                await asyncio.sleep(decision.delay)

    async def upload(self, url: str, data: bytes, headers: dict[str, str], timeout: float) -> None:
        state = RetryState(_upload_request(timeout))
        while True:
            attempt_timeout = state.attempt_timeout()
            if attempt_timeout is None:
                raise state.budget_error()
            try:
                response = await self._client.put(url, content=data, headers=headers, timeout=attempt_timeout)
            except httpx.HTTPError as exc:
                decision = state.on_exception(exc)
            else:
                outcome = state.on_response(response.status_code, response.headers, response.content)
                if outcome is None:
                    return
                decision = outcome
            if not decision.retry:
                assert decision.error is not None
                raise decision.error
            if decision.delay:
                await asyncio.sleep(decision.delay)

    async def _reconnect(self) -> None:
        # Other tasks may still be mid-request on the old pool, so it is retired, not closed.
        if not self._owns_client:
            return
        self._retired.append(self._client)
        self._client = httpx.AsyncClient()

    async def aclose(self) -> None:
        if not self._owns_client:
            return
        clients, self._retired = [*self._retired, self._client], []
        for client in clients:
            await client.aclose()
