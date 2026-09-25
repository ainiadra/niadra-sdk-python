"""A few lines of ASGI shared by the small services the harness runs: no framework, one dependency."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


async def read_body(receive: Receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += bytes(message.get("body", b""))
        if not message.get("more_body"):
            return body


async def respond(
    send: Send, status: int, body: bytes, headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers or []})
    await send({"type": "http.response.body", "body": body})


async def respond_json(send: Send, status: int, payload: Any) -> None:
    await respond(send, status, json.dumps(payload).encode(), [(b"content-type", b"application/json")])


async def lifespan(receive: Receive, send: Send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def request_headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
