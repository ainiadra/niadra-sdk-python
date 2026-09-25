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
from datetime import datetime
from typing import Any

from niadra.conversation import AsyncConversation, Conversation
from niadra.models.events import ContextStamp

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
    """Constant-time comparison of the UTF-8 bytes; missing values never match. Strings go in as bytes:
    `hmac.compare_digest` raises on a non-ASCII `str`, and a forged header must answer 401, not fail."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def mapping(value: Any) -> Mapping[str, Any]:
    """The value when it is a JSON object, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def restore_stamp(session: Session, etag: Any, injected_at: Any) -> None:
    """Stamps the agent's turns with the context an earlier request put in the prompt.

    A platform that keeps variables for a call (ElevenLabs, Vapi) hands back the pack's etag and
    the moment it went into the prompt, so the turns recorded after the call carry them.
    """
    moment = text(injected_at)
    if moment is None:
        return
    try:
        session.context_stamp = ContextStamp(etag=text(etag), injected_at=datetime.fromisoformat(moment))
    except ValueError:
        return
