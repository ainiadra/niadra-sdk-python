from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from niadra import AsyncNiadra, Niadra
from niadra._transport import RetryState
from niadra.options import CacheOptions, QueueOptions
from niadra_mock import MOCK_KEY, MockApp

KEY = "nia_sk_live_br1_acme_k1_s3cret"
BASE = "https://acme.br1.api.niadra.com"


def context_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": '<context source="niadra">Marina, prefers WhatsApp</context>',
        "variables": {"name": "Marina"},
        "version": "v7",
        "etag": "etag-1",
        "as_of": "2026-09-22T16:40:02Z",
        "verification": {"requested": "V1", "effective": "V1"},
        "withheld": 2,
        "live": [],
        "cache": {
            "breakpoints": [12],
            "cacheable": True,
            "salt": "s",
            "ttl_seconds": 300,
            "floor_tokens": 1024,
        },
        "timing": {"total": 4.2},
        "path": "t0",
    }
    payload.update(overrides)
    return payload


def batch_ok(accepted: int = 1) -> dict[str, Any]:
    return {"accepted": accepted, "duplicates": 0, "errors": []}


def fixed_clock() -> datetime:
    return datetime(2026, 9, 22, 14, 7, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RetryState, "_backoff", lambda self: 0.001)


@pytest.fixture(autouse=True)
def _nothing_leaves_after_the_test(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stops the background sender and refreshes of every sync client once its test ends.

    A client left open keeps a thread that retries its queue: a request it starts after its test
    would take an answer from the next test's mock. Nothing queued is sent at this point.
    """
    created: list[Niadra] = []
    init = Niadra.__init__

    def recording_init(self: Niadra, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(Niadra, "__init__", recording_init)
    yield
    for client in created:
        client._flusher.stop(timeout=0)  # joins the thread; a spent timeout sends nothing
        thread = client._flusher._thread
        if thread is not None:
            thread.join(timeout=10)
        if client._refresher is not None:
            client._refresher.shutdown(wait=True)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIADRA_API_KEY", raising=False)
    monkeypatch.delenv("NIADRA_BASE_URL", raising=False)


@pytest.fixture
def quiet_queue() -> QueueOptions:
    """A queue that never flushes on its own within a test, so tests drive `flush()`."""
    return QueueOptions(batch_size=10_000, interval=3600, turn_interval=3600)


@pytest.fixture
def client(quiet_queue: QueueOptions) -> Iterator[Niadra]:
    niadra = Niadra(KEY, channel="whatsapp", queue=quiet_queue, strict=True)
    yield niadra
    niadra._closed = True  # skip the flush at exit: respx routes are gone by then


@pytest.fixture
def lenient(quiet_queue: QueueOptions) -> Iterator[Niadra]:
    niadra = Niadra(KEY, channel="whatsapp", queue=quiet_queue)
    yield niadra
    niadra._closed = True


@pytest.fixture
def mock_app() -> MockApp:
    return MockApp()


@pytest.fixture
def on_mock(mock_app: MockApp, quiet_queue: QueueOptions) -> Iterator[Niadra]:
    http = httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi))
    niadra = Niadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="whatsapp",
        strict=True,
        queue=quiet_queue,
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=http,
    )
    yield niadra
    niadra.close()
    http.close()


@pytest.fixture
async def on_mock_async(mock_app: MockApp, quiet_queue: QueueOptions) -> AsyncIterator[AsyncNiadra]:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app.asgi))
    niadra = AsyncNiadra(
        MOCK_KEY,
        base_url="http://mock",
        channel="whatsapp",
        strict=True,
        queue=quiet_queue,
        cache=CacheOptions(ttl=0, stale_while_revalidate=0),
        http_client=http,
    )
    yield niadra
    await niadra.close()
    await http.aclose()


@pytest.fixture
def niadra_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="niadra")
    return caplog
