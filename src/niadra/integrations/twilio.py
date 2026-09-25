"""Twilio: calls and messages from Twilio's webhooks, with the carrier's attestation as proof.

```python
from niadra import Niadra
from niadra.integrations.twilio import parse_call, parse_message

niadra = Niadra(channel="voice")


@app.post("/twilio/voice")  # the number's "A call comes in" webhook
def incoming_call(request):
    call = parse_call(request.body, request.headers, str(request.url), TWILIO_AUTH_TOKEN)
    if call is None:
        return Response(status_code=403)  # not signed by Twilio with your auth token
    conversation = call.conversation(niadra)  # CallSid as the id, the caller's number as subject
    call.verify(conversation)  # StirVerstat: TN-Validation-Passed-A proves V2, B and C prove V1
    context = conversation.context()  # hand it to the agent that takes the call
    ...
```

- `parse_call()` reads a Programmable Voice webhook (the incoming call or a status callback):
  `CallSid`, `From`, `To`, `Direction`, `CallStatus` and `StirVerstat`. `call.conversation()` opens
  the Niadra conversation of the call, `call.verify()` records the attestation (a failed or
  missing validation proves nothing), and `call.ended(conversation)` ends it when the status
  callback says `completed` (or `busy`, `failed`, `no-answer`, `canceled`).
- `parse_message()` reads a Messaging webhook, SMS or WhatsApp through Twilio: `MessageSid` is the
  idempotency key, `From` (`whatsapp:+55...` or a phone) and `WaId` the sender's ids, `Body` the
  text. `message.record(conversation)` records the customer's turn with `verification_hint: "V1"`.
- Both check `X-Twilio-Signature`: the Base64 HMAC-SHA1, with your auth token, of the full URL
  Twilio called followed by every POST parameter, sorted by name, as name and value.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, cast, overload
from urllib.parse import parse_qsl

from niadra._async_client import AsyncNiadra
from niadra._client import Niadra
from niadra.conversation import AsyncConversation, Conversation
from niadra.handles import whatsapp
from niadra.integrations._common import attestation_level, end, phone_or_none, warn
from niadra.integrations._webhooks import Body, Session, header
from niadra.models.common import Handle
from niadra.models.events import VoiceInfo
from niadra.vocabulary import Verification

__all__ = ["TwilioCall", "TwilioMessage", "parse_call", "parse_message", "signature", "verify_signature"]

SIGNATURE_HEADER = "X-Twilio-Signature"
_FINAL = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})

Params = Mapping[str, str | list[str]]


def form(body: Body) -> dict[str, list[str]]:
    """The POST parameters of a form-encoded body, every value of each name kept."""
    if isinstance(body, Mapping):
        return {k: list(v) if isinstance(v, (list, tuple)) else [str(v)] for k, v in body.items()}
    text = body.decode() if isinstance(body, (bytes, bytearray)) else body
    params: dict[str, list[str]] = {}
    for key, value in parse_qsl(text, keep_blank_values=True):
        params.setdefault(key, []).append(value)
    return params


def signature(url: str, params: Params, auth_token: str) -> str:
    """The `X-Twilio-Signature` Twilio computes for a request to `url` with these POST parameters."""
    data = url
    for name in sorted(params):
        values = params[name]
        for value in sorted(values) if isinstance(values, list) else [values]:
            data += name + value
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_signature(url: str, params: Params, given: str | None, auth_token: str | None) -> bool:
    """Checks `X-Twilio-Signature`, with and without the default port in the URL, as Twilio signs."""
    if not given or not auth_token:
        return False
    candidates = {url, _without_port(url), _with_port(url)}
    return any(hmac.compare_digest(given, signature(url_, params, auth_token)) for url_ in candidates)


def _without_port(url: str) -> str:
    return url.replace(":443/", "/", 1) if url.startswith("https://") else url.replace(":80/", "/", 1)


def _with_port(url: str) -> str:
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    if ":" in host:
        return url
    return f"{scheme}://{host}:{443 if scheme == 'https' else 80}{slash}{path}"


def _first(params: Mapping[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    return values[0] if values and values[0] else None


@dataclass(frozen=True)
class TwilioCall:
    """One Programmable Voice webhook: the incoming call, or a status callback of it."""

    call_sid: str
    from_number: str | None = None
    to_number: str | None = None
    direction: str | None = None
    status: str | None = None
    attestation: str | None = None
    params: Mapping[str, list[str]] = field(default_factory=dict, repr=False)

    @property
    def caller(self) -> str | None:
        """The customer's number: `From` on an inbound call, `To` on an outbound one."""
        return self.to_number if (self.direction or "").startswith("outbound") else self.from_number

    @property
    def subject(self) -> Handle | None:
        return phone_or_none(self.caller)

    @property
    def level(self) -> Verification | None:
        """What `StirVerstat` proves: V2 for `TN-Validation-Passed-A`, V1 for B and C, else nothing."""
        return attestation_level(self.attestation)

    @property
    def finished(self) -> bool:
        return (self.status or "") in _FINAL

    def voice_info(self) -> VoiceInfo:
        """The call's voice details for an event: numbers and the attestation letter."""
        letter = self.attestation.rsplit("-", 1)[-1] if self.level is not None and self.attestation else None
        attested = cast(Literal["A", "B", "C"], letter) if letter in ("A", "B", "C") else None
        return VoiceInfo(ani=self.from_number, dnis=self.to_number, network_attestation=attested)

    @overload
    def conversation(
        self,
        niadra: Niadra,
        *,
        subject: Handle | None = None,
        channel: str = "voice",
        view: str = "voice",
        agent_id: str | None = None,
    ) -> Conversation: ...

    @overload
    def conversation(
        self,
        niadra: AsyncNiadra,
        *,
        subject: Handle | None = None,
        channel: str = "voice",
        view: str = "voice",
        agent_id: str | None = None,
    ) -> AsyncConversation: ...

    def conversation(
        self,
        niadra: Niadra | AsyncNiadra,
        *,
        subject: Handle | None = None,
        channel: str = "voice",
        view: str = "voice",
        agent_id: str | None = None,
    ) -> Session:
        """The Niadra conversation of this call: `CallSid` as its id, the caller as subject."""
        return niadra.conversation(
            self.call_sid, subject=subject or self.subject, channel=channel, view=view, agent_id=agent_id
        )

    def verify(self, session: Session) -> Any:
        """Records the attestation for the call; await it with `AsyncNiadra`. None when nothing is proven."""
        level = self.level
        if level is None or session.subject is None:
            return None
        try:
            return session.verify("network_attestation", level)
        except Exception as exc:
            warn("verify the call", exc)
            return None

    def ended(self, session: Session) -> bool:
        """Ends the conversation when this status callback is a final one. True when it did."""
        if not self.finished:
            return False
        end(session)
        return True


@dataclass(frozen=True)
class TwilioMessage:
    """One Messaging webhook: an SMS, or a WhatsApp message through Twilio."""

    message_sid: str
    from_address: str
    to_address: str | None = None
    body: str | None = None
    wa_id: str | None = None
    profile_name: str | None = None
    media: tuple[tuple[str, str | None], ...] = ()
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    params: Mapping[str, list[str]] = field(default_factory=dict, repr=False)

    @property
    def channel(self) -> str:
        return "whatsapp" if self.from_address.startswith("whatsapp:") else "sms"

    @property
    def handles(self) -> list[Handle]:
        found: list[Handle] = []
        if self.wa_id:
            with suppress(ValueError):
                found.append(whatsapp(self.wa_id))
        number = phone_or_none(self.from_address)
        if number is not None and self.channel == "sms":
            found.append(number)
        return found

    @property
    def subject(self) -> Handle | None:
        handles = self.handles
        return handles[0] if handles else phone_or_none(self.from_address)

    def record(self, session: Session) -> bool:
        """Records the customer's turn, keyed by `MessageSid`, with `verification_hint: "V1"`."""
        if not self.body:
            return False
        try:
            queued = session.customer(
                self.body,
                handles=[h for h in self.handles if h != session.subject],
                idempotency_key=self.message_sid,
                verification_hint=Verification.V1,
            )
            if queued and session.verification.rank < Verification.V1.rank:
                session._verified(Verification.V1)
            return queued
        except Exception as exc:
            warn("record the Twilio message", exc)
            return False


def parse_call(
    body: Body, headers: Mapping[str, str] | None, url: str, auth_token: str | None
) -> TwilioCall | None:
    """A signed Programmable Voice webhook as a `TwilioCall`; None when the signature is wrong."""
    params = form(body)
    if not verify_signature(url, params, header(headers, SIGNATURE_HEADER), auth_token):
        return None
    call_sid = _first(params, "CallSid")
    if call_sid is None:
        return None
    return TwilioCall(
        call_sid=call_sid,
        from_number=_first(params, "From"),
        to_number=_first(params, "To"),
        direction=_first(params, "Direction"),
        status=_first(params, "CallStatus"),
        attestation=_first(params, "StirVerstat"),
        params=params,
    )


def parse_message(
    body: Body, headers: Mapping[str, str] | None, url: str, auth_token: str | None
) -> TwilioMessage | None:
    """A signed Messaging webhook as a `TwilioMessage`; None when the signature is wrong."""
    params = form(body)
    if not verify_signature(url, params, header(headers, SIGNATURE_HEADER), auth_token):
        return None
    sid = _first(params, "MessageSid") or _first(params, "SmsMessageSid")
    sender = _first(params, "From")
    if sid is None or sender is None:
        return None
    count = int(_first(params, "NumMedia") or 0)
    media = tuple(
        (url_, _first(params, f"MediaContentType{i}"))
        for i in range(count)
        if (url_ := _first(params, f"MediaUrl{i}")) is not None
    )
    return TwilioMessage(
        message_sid=sid,
        from_address=sender,
        to_address=_first(params, "To"),
        body=_first(params, "Body"),
        wa_id=_first(params, "WaId"),
        profile_name=_first(params, "ProfileName"),
        media=media,
        params=params,
    )
