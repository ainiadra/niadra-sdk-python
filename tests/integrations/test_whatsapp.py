"""WhatsApp Cloud API: webhooks recorded in Meta's public format, signatures computed here, and the
emulator behind."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from niadra import AsyncNiadra, Niadra, whatsapp, whatsapp_bsuid
from niadra.integrations.whatsapp import parse_webhook, sent, subscribe, verify_signature
from niadra_mock import MockApp
from tests.integrations.support import events, turns

PAYLOADS = Path(__file__).parent / "payloads"
APP_SECRET = "meta-app-secret"
MARINA_WA = whatsapp("5511912345678")
MARINA_BSUID = whatsapp_bsuid("BR.1098341", "301884756320117")
TEXT_ID = "wamid.HBgNNTUxMTkxMjM0NTY3OBUCABIYFDNBMTIzNDU2Nzg5MEFCQ0RFRjAA"


def payload(name: str) -> bytes:
    return (PAYLOADS / f"whatsapp_{name}.json").read_bytes()


def signed(body: bytes, secret: str = APP_SECRET) -> dict[str, str]:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}


def test_a_signed_webhook_becomes_messages_oldest_first() -> None:
    body = payload("messages")
    messages = parse_webhook(body, signed(body), APP_SECRET)
    assert messages is not None
    text, voice = messages
    assert (text.type, text.text, text.message_id) == (
        "text",
        "The technician never showed up. I am calling you.",
        TEXT_ID,
    )
    assert text.occurred_at.timestamp() == 1790301660
    assert (text.wa_id, text.profile_name, text.phone_number_id) == (
        "5511912345678",
        "Marina Souza",
        "106540352242922",
    )
    assert text.handles == [MARINA_WA, MARINA_BSUID]
    assert text.subject == MARINA_WA
    assert voice.media is not None and voice.media.voice and voice.media.mime_type == "audio/ogg; codecs=opus"


def test_an_unsigned_or_tampered_webhook_is_refused() -> None:
    body = payload("messages")
    assert parse_webhook(body, {}, APP_SECRET) is None
    assert parse_webhook(body, signed(body, "other"), APP_SECRET) is None
    assert parse_webhook(body.replace(b"never", b"always"), signed(body), APP_SECRET) is None
    assert parse_webhook(body, signed(body), None) is None
    assert not verify_signature(body, "sha1=abc", APP_SECRET)


def test_statuses_are_not_messages() -> None:
    body = payload("statuses")
    assert parse_webhook(body, signed(body), APP_SECRET) == []


def test_the_subscription_check() -> None:
    query = {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "1158201444"}
    answer = subscribe(query, "tok")
    assert (answer.status, answer.text(), answer.content_type) == (200, "1158201444", "text/plain")
    assert subscribe({**query, "hub.verify_token": "guess"}, "tok").status == 403


def test_a_message_is_the_customers_turn_and_proves_v1(on_mock: Niadra, mock_app: MockApp) -> None:
    body = payload("messages")
    text, _voice = parse_webhook(body, signed(body), APP_SECRET) or []
    with on_mock.conversation("wa-5511912345678", subject=text.subject) as chat:
        assert text.record(chat)
        assert chat.verification.value == "V1"
        on_mock.flush()
        context = chat.context()
        assert context.verification.effective.value == "V1"
        assert sent(chat, "Sorry about that, I am rescheduling now.", {"messages": [{"id": "wamid.OUT1"}]})
        assert text.record(chat), "a redelivery is queued, and the API drops it by its key"
    on_mock.flush()
    assert turns(mock_app.cell, "wa-5511912345678") == [
        ("customer", "The technician never showed up. I am calling you."),
        ("ai_agent", "Sorry about that, I am rescheduling now."),
    ]
    customer, agent = events(mock_app.cell, "wa-5511912345678")[:2]
    assert customer.idempotency_key == TEXT_ID and agent.idempotency_key == "wamid.OUT1"
    assert customer.handles == [MARINA_WA, MARINA_BSUID]
    assert customer.verification_hint is not None and customer.verification_hint.value == "V1"
    assert agent.verification_hint is None
    assert mock_app.cell.same_profile(MARINA_WA, MARINA_BSUID)


async def test_a_voice_note_goes_by_reference_with_its_transcript(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    body = payload("messages")
    _text, voice = parse_webhook(body, signed(body), APP_SECRET) or []
    async with on_mock_async.conversation("wa-5511912345678", subject=voice.subject) as chat:
        assert not voice.record(chat), "media needs its upload first"
        upload = await on_mock_async.upload_media(b"OggS...", "audio/ogg", subject=voice.subject)
        assert upload is not None
        assert voice.record(
            chat, upload=upload, transcript="He was supposed to come at eight.", stt_confidence=0.93
        )
    await on_mock_async.flush()
    (event,) = [e for e in events(mock_app.cell, "wa-5511912345678") if e.kind.value == "message"]
    assert event.content is not None
    assert (event.content.type, event.content.media_ref, event.content.stt_confidence) == (
        "audio",
        upload.media_ref,
        0.93,
    )
    assert event.content.transcript == "He was supposed to come at eight."


class Broken:
    subject = None

    def customer(self, *args: object, **kwargs: object) -> bool:
        raise RuntimeError("down")

    def agent(self, *args: object, **kwargs: object) -> bool:
        raise RuntimeError("down")


def test_recording_never_raises(on_mock: Niadra) -> None:
    body = payload("messages")
    text, _ = parse_webhook(body, signed(body), APP_SECRET) or []
    assert text.record(Broken()) is False  # type: ignore[arg-type]
    assert sent(Broken(), "hi", {"messages": []}) is False  # type: ignore[arg-type]
    chat = on_mock.conversation("wa-1", subject=MARINA_WA)
    assert sent(chat, "hi", object()) is True, "an odd response still records"


def test_a_signature_with_non_ascii_characters_is_refused_not_raised() -> None:
    # `hmac.compare_digest` raises on a non-ASCII str; a forged header must be a 401, not a 500.
    body = b'{"object": "whatsapp_business_account"}'
    assert not verify_signature(body, "sha256=é" * 3, APP_SECRET)
    assert parse_webhook(body, {"X-Hub-Signature-256": "sha256=é"}, APP_SECRET) is None
