"""Governance calls for a key with the `admin` scope: find a customer, read their memory and a fact's
history, correct it, erase it, export it. `niadra.admin` (sync) and `AsyncNiadra.admin` (async).

The requests are built here, without I/O; each client sends them with its own transport. Like the rest of
the SDK they fail open: a failure logs and returns None (or an empty list), and raises under `strict`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

from pydantic import BaseModel

from niadra._base import ClientCore, HandleLike, as_handle
from niadra._ids import new_key
from niadra._transport import Request
from niadra.models.admin import (
    CorrectionAction,
    CorrectionRequest,
    Erasure,
    ExportPackage,
    ExportRequest,
    FactHistory,
    ForgetRequest,
    ProfileMatch,
    ProfileMemory,
    ProfileSearchResult,
)
from niadra.models.events import BatchResponse

if TYPE_CHECKING:
    from niadra._transport import AsyncTransport, SyncTransport

M = TypeVar("M", bound=BaseModel)
CorrectionLike = CorrectionRequest | Mapping[str, Any]


def _path(value: str) -> str:
    return quote(str(value), safe="")


class _Requests:
    def __init__(self, core: ClientCore) -> None:
        self._core = core

    def _write(self, method: str, path: str, body: Any = None, key: str | None = None) -> Request:
        budget = self._core.timeouts.write
        return Request(method, path, json=body, timeout=budget, budget=budget, idempotency_key=key)

    def find_profiles(self, query: str, type: str | None, scope: str | None, limit: int) -> Request:
        body = {
            "query": query,
            "limit": limit,
            **({"type": type} if type else {}),
            **({"scope": scope} if scope else {}),
        }
        return self._write("POST", "/v1/profiles/search", body)

    def memory(self, profile_id: str) -> Request:
        return self._write("GET", f"/v1/profiles/{_path(profile_id)}/memory")

    def fact_history(self, profile_id: str, fact_id: str) -> Request:
        fact = fact_id.removeprefix("fact:")
        return self._write("GET", f"/v1/profiles/{_path(profile_id)}/facts/{_path(fact)}/history")

    def correct(self, request: CorrectionRequest, key: str) -> Request:
        return self._write("POST", "/v1/corrections", request.model_dump(mode="json", exclude_none=True), key)

    def correct_batch(self, items: Sequence[CorrectionLike], key: str) -> Request:
        body = {"items": [_correction(i).model_dump(mode="json", exclude_none=True) for i in items]}
        return self._write("POST", "/v1/corrections/batch", body, key)

    def forget(self, request: ForgetRequest, key: str) -> Request:
        return self._write("POST", "/v1/forget", request.model_dump(mode="json", exclude_none=True), key)

    def forget_status(self, request_id: str) -> Request:
        return self._write("GET", f"/v1/forget/{_path(request_id)}")

    def export(self, request: ExportRequest) -> Request:
        return self._write(
            "POST", "/v1/export", request.model_dump(mode="json", exclude_none=True), new_key()
        )


def _correction(value: CorrectionLike) -> CorrectionRequest:
    return value if isinstance(value, CorrectionRequest) else CorrectionRequest.model_validate(value)


def _forget(profile_id: str | None, handle: HandleLike | None, conversation_id: str | None) -> ForgetRequest:
    named = [
        n for n, v in (("profile", profile_id), ("handle", handle), ("conversation", conversation_id)) if v
    ]
    if len(named) != 1:
        raise ValueError("pass exactly one of profile_id, handle or conversation_id")
    return ForgetRequest(
        target=named[0],
        profile_id=profile_id,
        handle=as_handle(handle) if handle is not None else None,
        conversation_id=conversation_id,
    )


def _export(profile_id: str | None, handle: HandleLike | None) -> ExportRequest:
    if (profile_id is None) == (handle is None):
        raise ValueError("pass exactly one of profile_id or handle")
    return ExportRequest(profile_id=profile_id, handle=as_handle(handle) if handle is not None else None)


class Admin:
    """Governance calls of the synchronous client: `niadra.admin`. Needs a key with the `admin` scope."""

    def __init__(self, core: ClientCore, transport: SyncTransport) -> None:
        self._core = core
        self._transport = transport
        self._requests = _Requests(core)

    def _send(self, name: str, build: Any, model: type[M]) -> M | None:
        if not self._core.enabled:
            return None
        try:
            return model.model_validate(self._transport.request(build()))
        except Exception as exc:
            return self._core.fail(f"admin.{name}", exc, None)

    def find_profiles(
        self, query: str, *, type: str | None = None, scope: str | None = None, limit: int = 20
    ) -> list[ProfileMatch]:
        """Profiles by pseudonym (`p_...`) or identifier, the value in the body; handles masked by role."""
        found = self._send(
            "find_profiles",
            lambda: self._requests.find_profiles(query, type, scope, limit),
            ProfileSearchResult,
        )
        return found.items if found else []

    def memory(self, profile_id: str) -> ProfileMemory | None:
        """Everything memory holds about one customer, every status, with provenance; leaves a receipt."""
        return self._send("memory", lambda: self._requests.memory(profile_id), ProfileMemory)

    def fact_history(self, profile_id: str, fact_id: str) -> FactHistory | None:
        """Every value a fact's slot held, oldest first, with who replaced whom."""
        return self._send(
            "fact_history", lambda: self._requests.fact_history(profile_id, fact_id), FactHistory
        )

    def correct(
        self,
        profile_id: str,
        action: CorrectionAction,
        *,
        fact_id: str | None = None,
        open_item_id: str | None = None,
        value: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> BatchResponse | None:
        """The data subject's right to correction: retract or correct a fact, or resolve an open item."""

        def build() -> Request:
            request = CorrectionRequest(
                profile_id=profile_id,
                action=action,
                fact_id=fact_id.removeprefix("fact:") if fact_id else None,
                open_item_id=open_item_id,
                value=value,
                reason=reason,
            )
            return self._requests.correct(request, idempotency_key or new_key())

        return self._send("correct", build, BatchResponse)

    def correct_batch(
        self, items: Sequence[CorrectionLike], *, idempotency_key: str | None = None
    ) -> BatchResponse | None:
        """Up to 100 corrections; item `n` is keyed `<idempotency_key>:<n>`, so a retry repeats none."""
        key = idempotency_key or new_key()
        return self._send("correct_batch", lambda: self._requests.correct_batch(items, key), BatchResponse)

    def forget(
        self,
        *,
        profile_id: str | None = None,
        handle: HandleLike | None = None,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Erasure | None:
        """Erases a customer, one identifier or one conversation, with a receipt. Follow `forget_status`."""
        key = idempotency_key or new_key()
        return self._send(
            "forget",
            lambda: self._requests.forget(_forget(profile_id, handle, conversation_id), key),
            Erasure,
        )

    def forget_status(self, request_id: str) -> Erasure | None:
        return self._send("forget_status", lambda: self._requests.forget_status(request_id), Erasure)

    def export(
        self, *, profile_id: str | None = None, handle: HandleLike | None = None
    ) -> ExportPackage | None:
        """One customer's portable package: JSON lines per kind of record, zipped, behind a signed link."""
        return self._send("export", lambda: self._requests.export(_export(profile_id, handle)), ExportPackage)


class AsyncAdmin:
    """Governance calls of the async client: `AsyncNiadra.admin`. See `Admin`."""

    def __init__(self, core: ClientCore, transport: AsyncTransport) -> None:
        self._core = core
        self._transport = transport
        self._requests = _Requests(core)

    async def _send(self, name: str, build: Any, model: type[M]) -> M | None:
        if not self._core.enabled:
            return None
        try:
            return model.model_validate(await self._transport.request(build()))
        except Exception as exc:
            return self._core.fail(f"admin.{name}", exc, None)

    async def find_profiles(
        self, query: str, *, type: str | None = None, scope: str | None = None, limit: int = 20
    ) -> list[ProfileMatch]:
        found = await self._send(
            "find_profiles",
            lambda: self._requests.find_profiles(query, type, scope, limit),
            ProfileSearchResult,
        )
        return found.items if found else []

    async def memory(self, profile_id: str) -> ProfileMemory | None:
        return await self._send("memory", lambda: self._requests.memory(profile_id), ProfileMemory)

    async def fact_history(self, profile_id: str, fact_id: str) -> FactHistory | None:
        return await self._send(
            "fact_history", lambda: self._requests.fact_history(profile_id, fact_id), FactHistory
        )

    async def correct(
        self,
        profile_id: str,
        action: CorrectionAction,
        *,
        fact_id: str | None = None,
        open_item_id: str | None = None,
        value: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> BatchResponse | None:
        def build() -> Request:
            request = CorrectionRequest(
                profile_id=profile_id,
                action=action,
                fact_id=fact_id.removeprefix("fact:") if fact_id else None,
                open_item_id=open_item_id,
                value=value,
                reason=reason,
            )
            return self._requests.correct(request, idempotency_key or new_key())

        return await self._send("correct", build, BatchResponse)

    async def correct_batch(
        self, items: Sequence[CorrectionLike], *, idempotency_key: str | None = None
    ) -> BatchResponse | None:
        key = idempotency_key or new_key()
        return await self._send(
            "correct_batch", lambda: self._requests.correct_batch(items, key), BatchResponse
        )

    async def forget(
        self,
        *,
        profile_id: str | None = None,
        handle: HandleLike | None = None,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Erasure | None:
        key = idempotency_key or new_key()
        return await self._send(
            "forget",
            lambda: self._requests.forget(_forget(profile_id, handle, conversation_id), key),
            Erasure,
        )

    async def forget_status(self, request_id: str) -> Erasure | None:
        return await self._send("forget_status", lambda: self._requests.forget_status(request_id), Erasure)

    async def export(
        self, *, profile_id: str | None = None, handle: HandleLike | None = None
    ) -> ExportPackage | None:
        return await self._send(
            "export", lambda: self._requests.export(_export(profile_id, handle)), ExportPackage
        )
