"""What the route methods of `niadra.api` share: building each request and sending it.

The methods themselves are generated from the server's OpenAPI document (`scripts/sync_spec.py`); this is
the part that is written by hand. Every method raises: the calls built on them decide, per purpose, what
fails open and what fails closed.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

from pydantic import BaseModel

from niadra._transport import Request
from niadra.errors import ConfigurationError

if TYPE_CHECKING:
    from niadra._base import ClientCore
    from niadra._transport import AsyncTransport, SyncTransport

M = TypeVar("M", bound=BaseModel)


def segment(value: str) -> str:
    """A path parameter, escaped whole: an id may hold `/` or `:`."""
    return quote(str(value), safe="")


def _request(
    core: ClientCore,
    method: str,
    path: str,
    body: BaseModel | None,
    params: dict[str, Any] | None,
    key: str | None,
) -> Request:
    if not core.enabled:
        raise ConfigurationError("the client has no usable API key")
    budget = core.timeouts.write
    query = {
        k: v.isoformat() if isinstance(v, date) else v for k, v in (params or {}).items() if v is not None
    }
    # What the caller set, by the wire names: a default the caller left alone is the server's to apply.
    payload = None if body is None else body.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return Request(
        method, path, json=payload, params=query or None, timeout=budget, budget=budget, idempotency_key=key
    )


class SyncRoutes:
    def __init__(self, core: ClientCore, transport: SyncTransport) -> None:
        self._core = core
        self._transport = transport

    def _call(
        self,
        model: type[M],
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        params: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> M:
        request = _request(self._core, method, path, body, params, key)
        return model.model_validate(self._transport.request(request))

    def _send(self, method: str, path: str, *, body: BaseModel | None = None, key: str | None = None) -> None:
        self._transport.request(_request(self._core, method, path, body, None, key))


class AsyncRoutes:
    def __init__(self, core: ClientCore, transport: AsyncTransport) -> None:
        self._core = core
        self._transport = transport

    async def _call(
        self,
        model: type[M],
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        params: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> M:
        request = _request(self._core, method, path, body, params, key)
        return model.model_validate(await self._transport.request(request))

    async def _send(
        self, method: str, path: str, *, body: BaseModel | None = None, key: str | None = None
    ) -> None:
        await self._transport.request(_request(self._core, method, path, body, None, key))
