"""The fault proxy of metric 7: in front of a memory service, it either holds every request for a fixed
delay before forwarding it, or answers a fixed error status without forwarding. The same proxy, with
the same settings, stands in front of Niadra and of Mem0.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from niadra_bench.services.asgi import (
    Receive,
    Scope,
    Send,
    lifespan,
    read_body,
    request_headers,
    respond,
    respond_json,
)

_HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}


@dataclass
class Fault:
    delay_ms: int = 0
    status: int | None = None

    @property
    def name(self) -> str:
        return f"http_{self.status}" if self.status else f"delay_{self.delay_ms}ms"


class FaultProxy:
    def __init__(
        self, upstream: str, fault: Fault, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.fault = fault
        self._client = httpx.AsyncClient(transport=transport, timeout=60.0)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        body = await read_body(receive)
        if self.fault.status:
            await respond_json(send, self.fault.status, {"status": self.fault.status, "code": "injected"})
            return
        if self.fault.delay_ms:
            await asyncio.sleep(self.fault.delay_ms / 1000)
        headers = {k: v for k, v in request_headers(scope).items() if k not in _HOP}
        query = scope.get("query_string", b"").decode()
        url = f"{self.upstream}{scope['path']}" + (f"?{query}" if query else "")
        upstream = await self._client.request(scope["method"], url, content=body, headers=headers)
        out_headers = [
            (k.encode(), v.encode())
            for k, v in upstream.headers.items()
            if k.lower() not in _HOP and k.lower() != "content-encoding"
        ]
        await respond(send, upstream.status_code, upstream.content, out_headers)
