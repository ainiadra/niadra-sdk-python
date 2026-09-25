"""WhatsApp Cloud API (Meta): the webhook's messages as Niadra turns. It never sends anything.

```python
from niadra import Niadra
from niadra.integrations.whatsapp import parse_webhook, sent, subscribe

niadra = Niadra(channel="whatsapp")


@app.get("/whatsapp")
def challenge(request):  # Meta's subscription check
    answer = subscribe(request.query_params, VERIFY_TOKEN)
    return Response(answer.text(), answer.status, media_type=answer.content_type)


@app.post("/whatsapp")
def inbound(request):
    messages = parse_webhook(request.body, request.headers, APP_SECRET)
    if messages is None:
        return Response(status_code=401)  # not signed by Meta with your app secret
    for message in messages:
        with niadra.conversation(f"wa-{message.wa_id}", subject=message.subject, view="chat") as chat:
            message.record(chat)  # the customer's turn, with the wamid as idempotency key
            niadra.flush()  # the turn, and the V1 it proves, land before the first read
            context = chat.context()
            reply = your_model(context.system_block, context.turn_block, message.text)
            response = graph.send_text(message.wa_id, reply)  # your call to the Cloud API
            sent(chat, reply, response)  # the agent's turn, keyed by the wamid Meta returned
    return Response(status_code=200)
```

- `parse_webhook()` checks `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app
  secret) and returns the messages of every entry and change, oldest first; statuses and other
  fields are left out. Each `WhatsAppMessage` carries the `wa_id` and, for a WhatsApp username, the
  business-scoped user id (a BSUID, scoped to the business account), the profile name, the text
  (body, caption, button or list reply) and the media reference (`id`, MIME type, SHA-256).
- `message.record(session)` records the customer's turn with the `wamid` as idempotency key (Meta
  retries webhooks for days), the time it was sent, every id of the sender, and
  `verification_hint: "V1"`: it came from that number. It raises the session's level to V1 for
  the next read. For media, download the bytes from the Graph API, hand them to `upload_media()`,
  and pass the result as `upload=` (with your `transcript=` of a voice note).
- `sent(session, text, response)` records the agent's turn with the `wamid` the Cloud API returned.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from niadra.handles import whatsapp, whatsapp_bsuid
from niadra.integrations._common import warn
from niadra.integrations._webhooks import (
    Body,
    Session,
    WebhookResponse,
    header,
    mapping,
    parse,
    raw,
    same,
    text,
)
from niadra.models.common import Handle
from niadra.models.events import Content
from niadra.models.results import MediaUpload
from niadra.vocabulary import Verification

__all__ = ["WhatsAppMedia", "WhatsAppMessage", "parse_webhook", "sent", "subscribe", "verify_signature"]

SIGNATURE_HEADER = "X-Hub-Signature-256"
_MEDIA = ("image", "audio", "video", "document", "sticker")
_CONTENT_TYPES: dict[str, Literal["image", "audio", "file"]] = {
    "image": "image",
    "sticker": "image",
    "audio": "audio",
    "video": "file",
    "document": "file",
}


@dataclass(frozen=True)
class WhatsAppMedia:
    """A media reference from the webhook: fetch the bytes from the Graph API with `id`."""

    id: str
    mime_type: str | None = None
    sha256: str | None = None
    caption: str | None = None
    filename: str | None = None
    voice: bool = False


@dataclass(frozen=True)
class WhatsAppMessage:
    """One inbound message of the Cloud API webhook."""

    message_id: str
    type: str
    occurred_at: datetime
    wa_id: str | None = None
    user_id: str | None = None
    business_account: str | None = None
    phone_number_id: str | None = None
    profile_name: str | None = None
    text: str | None = None
    media: WhatsAppMedia | None = None
    reply_to: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def handles(self) -> list[Handle]:
        """Every id of the sender: the `wa_id` and, with a username, the BSUID in its account."""
        found: list[Handle] = []
        if self.wa_id:
            with suppress(ValueError):
                found.append(whatsapp(self.wa_id))
        if self.user_id and self.business_account:
            found.append(whatsapp_bsuid(self.user_id, self.business_account))
        return found

    @property
    def subject(self) -> Handle | None:
        """The handle to open the conversation with: the `wa_id`, else the BSUID."""
        handles = self.handles
        return handles[0] if handles else None

    def content(
        self,
        upload: MediaUpload | None = None,
        *,
        transcript: str | None = None,
        stt_confidence: float | None = None,
    ) -> Content:
        """The event's content: the text, or the uploaded media with its caption or transcript."""
        if upload is None or self.media is None:
            return Content(
                text=self.text or transcript, stt_confidence=stt_confidence if transcript else None
            )
        return Content(
            type=_CONTENT_TYPES.get(self.type, "file"),
            text=self.media.caption,
            media_ref=upload.media_ref,
            media_sha256=upload.media_sha256,
            transcript=transcript,
            stt_confidence=stt_confidence,
        )

    def record(
        self,
        session: Session,
        *,
        upload: MediaUpload | None = None,
        transcript: str | None = None,
        stt_confidence: float | None = None,
    ) -> bool:
        """Records the customer's turn in `session`. Never raises; False when it was not queued."""
        try:
            content = self.content(upload, transcript=transcript, stt_confidence=stt_confidence)
            if not (content.text or content.media_ref or content.transcript):
                return False
            queued = session.customer(
                content.text or content.transcript or "",
                handles=[h for h in self.handles if h != session.subject],
                content=content,
                idempotency_key=self.message_id,
                occurred_at=self.occurred_at,
                verification_hint=Verification.V1,
            )
            if queued and session.verification.rank < Verification.V1.rank:
                session._verified(Verification.V1)
            return queued
        except Exception as exc:
            warn("record the WhatsApp message", exc)
            return False


def verify_signature(body: Body, signature: str | None, app_secret: str | None) -> bool:
    """Checks `X-Hub-Signature-256`: `sha256=` and the hex HMAC-SHA256 of the raw body."""
    if not signature or not app_secret or not signature.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw(body), hashlib.sha256).hexdigest()
    return same(signature[len("sha256=") :], expected)


def subscribe(query: Mapping[str, Any], verify_token: str) -> WebhookResponse:
    """Answers Meta's subscription check (`hub.mode`, `hub.verify_token`, `hub.challenge`)."""
    if query.get("hub.mode") == "subscribe" and same(text(query.get("hub.verify_token")), verify_token):
        return WebhookResponse(200, str(query.get("hub.challenge", "")), "text/plain")
    return WebhookResponse(403, {"error": "verification failed"})


def parse_webhook(
    body: Body, headers: Mapping[str, str] | None, app_secret: str | None
) -> list[WhatsAppMessage] | None:
    """The inbound messages of a signed webhook, oldest first; None when the signature is wrong."""
    if not verify_signature(body, header(headers, SIGNATURE_HEADER), app_secret):
        return None
    payload = parse(body) or {}
    messages: list[WhatsAppMessage] = []
    for entry in payload.get("entry") or []:
        entry = mapping(entry)
        for change in entry.get("changes") or []:
            value = mapping(mapping(change).get("value"))
            contacts = [mapping(c) for c in value.get("contacts") or []]
            for item in value.get("messages") or []:
                message = _message(mapping(item), value, contacts, text(entry.get("id")))
                if message is not None:
                    messages.append(message)
    return sorted(messages, key=lambda m: m.occurred_at)


def sent(session: Session, reply: str, response: Any = None) -> bool:
    """Records the agent's turn sent through the Cloud API, keyed by the `wamid` it returned."""
    try:
        sent_ids = mapping(response).get("messages") if response is not None else None
        first = mapping(sent_ids[0]) if isinstance(sent_ids, list) and sent_ids else {}
        extra: dict[str, Any] = {}
        if message_id := text(first.get("id")):
            extra["idempotency_key"] = message_id
        return session.agent(reply, **extra)
    except Exception as exc:
        warn("record the agent's WhatsApp message", exc)
        return False


def _message(
    item: Mapping[str, Any], value: Mapping[str, Any], contacts: list[Mapping[str, Any]], account: str | None
) -> WhatsAppMessage | None:
    message_id, kind = text(item.get("id")), text(item.get("type"))
    if message_id is None or kind is None:
        return None
    sender = text(item.get("from"))
    user_id = text(item.get("from_user_id"))
    contact = next(
        (
            c
            for c in contacts
            if (sender and c.get("wa_id") == sender) or (user_id and c.get("user_id") == user_id)
        ),
        contacts[0] if len(contacts) == 1 else {},
    )
    stamp = item.get("timestamp")
    occurred = (
        datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        if isinstance(stamp, (str, int)) and str(stamp).isdigit()
        else datetime.now(timezone.utc)
    )
    media = None
    if kind in _MEDIA:
        body = mapping(item.get(kind))
        if media_id := text(body.get("id")):
            media = WhatsAppMedia(
                id=media_id,
                mime_type=text(body.get("mime_type")),
                sha256=text(body.get("sha256")),
                caption=text(body.get("caption")),
                filename=text(body.get("filename")),
                voice=bool(body.get("voice")),
            )
    return WhatsAppMessage(
        message_id=message_id,
        type=kind,
        occurred_at=occurred,
        wa_id=sender or text(contact.get("wa_id")),
        user_id=user_id or text(contact.get("user_id")),
        business_account=account,
        phone_number_id=text(mapping(value.get("metadata")).get("phone_number_id")),
        profile_name=text(mapping(contact.get("profile")).get("name")),
        text=_text(item, kind),
        media=media,
        reply_to=text(mapping(item.get("context")).get("id")),
        raw=item,
    )


def _text(item: Mapping[str, Any], kind: str) -> str | None:
    if kind == "text":
        return text(mapping(item.get("text")).get("body"))
    if kind == "button":
        return text(mapping(item.get("button")).get("text"))
    if kind == "interactive":
        interactive = mapping(item.get("interactive"))
        reply = mapping(interactive.get("button_reply")) or mapping(interactive.get("list_reply"))
        return text(reply.get("title"))
    if kind in _MEDIA:
        return text(mapping(item.get(kind)).get("caption"))
    if kind == "location":
        place = mapping(item.get("location"))
        return text(place.get("name")) or text(place.get("address"))
    return None
