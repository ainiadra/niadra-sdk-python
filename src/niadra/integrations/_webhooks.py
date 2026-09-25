"""What the webhook adapters (ElevenLabs, Vapi, WhatsApp, Twilio) share.

Each handler is a plain function of the request (raw body and headers) that returns a
`WebhookResponse`, so it fits any web framework: FastAPI, Flask, Django, a Lambda. The handlers
take `Niadra` or `AsyncNiadra`; with `Niadra`, call the `*_sync` twin or use `run_sync`.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from niadra.conversation import AsyncConversation, Conversation

Body = bytes | bytearray | str | Mapping[str, Any]
Session = Conversation | AsyncConversation


@dataclass(frozen=True)
class WebhookResponse:
    """What to answer the platform: an HTTP status and a JSON body (or plain text, for TwiML)."""

    status: int
    body: Any = field(default_factory=dict)
    content_type: str = "application/json"

    def text(self) -> str:
        """The body as the text to send."""
        if isinstance(self.body, str):
            return self.body
        return json.dumps(self.body, ensure_ascii=False, separators=(",", ":"))


OK = WebhookResponse(200, {"received": True})
UNAUTHORIZED = WebhookResponse(401, {"error": "invalid signature"})
BAD_REQUEST = WebhookResponse(400, {"error": "malformed request"})


def raw(body: Body) -> bytes:
    """The body as bytes: signatures are computed over the exact bytes received."""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body, separators=(",", ":")).encode()


def parse(body: Body) -> dict[str, Any] | None:
    """A JSON object from the body, or None when it is not one."""
    if isinstance(body, Mapping):
        return dict(body)
    try:
        value = json.loads(raw(body) or b"null")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def header(headers: Mapping[str, str] | None, name: str) -> str | None:
    """A header by name, whatever the case the framework keeps them in."""
    if not headers:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def same(given: str | None, expected: str | None) -> bool:
    """Constant-time comparison; missing values never match."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
