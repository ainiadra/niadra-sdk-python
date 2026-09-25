"""Twilio: voice and messaging webhooks recorded in Twilio's public format, signatures computed with
Twilio's documented algorithm, and the emulator behind."""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlencode

from niadra import AsyncNiadra, Niadra, phone, whatsapp
from niadra.integrations.twilio import parse_call, parse_message, signature, verify_signature
from niadra_mock import MockApp
from tests.integrations.support import MARINA, events, items, seed, turns

TOKEN = "12345678901234567890123456789012"
VOICE_URL = "https://agent.example.com/twilio/voice"
SMS_URL = "https://agent.example.com/twilio/messages"
CALL_SID = "CA7c9e2b4d6f8a0c1e3b5d7f9a1c3e5b7d"

INCOMING = {
    "AccountSid": "ACtest-account",
    "ApiVersion": "2010-04-01",
    "CallSid": CALL_SID,
    "CallStatus": "ringing",
    "Called": "+551130001000",
    "Caller": "+5511912345678",
    "Direction": "inbound",
    "From": "+5511912345678",
    "FromCountry": "BR",
    "StirVerstat": "TN-Validation-Passed-A",
    "To": "+551130001000",
    "ToCountry": "BR",
}
WHATSAPP = {
    "AccountSid": "ACtest-account",
    "ApiVersion": "2010-04-01",
    "Body": "Is my replacement lid on its way?",
    "From": "whatsapp:+5511912345678",
    "MessageSid": "SM1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
    "NumMedia": "1",
    "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/AC0f/Messages/SM1a/Media/ME9f",
    "MediaContentType0": "image/jpeg",
    "ProfileName": "Marina Souza",
    "SmsMessageSid": "SM1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
    "To": "whatsapp:+551130001000",
    "WaId": "5511912345678",
}


def twilio_signed(url: str, params: dict[str, str]) -> dict[str, str]:
    # Twilio's documented algorithm, written out independently of the module under test.
    data = url + "".join(name + params[name] for name in sorted(params))
    digest = hmac.new(TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return {"X-Twilio-Signature": base64.b64encode(digest).decode()}


def test_the_signature_matches_twilios_algorithm_and_its_port_variants() -> None:
    header = twilio_signed(VOICE_URL, INCOMING)["X-Twilio-Signature"]
    assert signature(VOICE_URL, INCOMING, TOKEN) == header
    assert verify_signature("https://agent.example.com:443/twilio/voice", INCOMING, header, TOKEN)
    assert not verify_signature(VOICE_URL, {**INCOMING, "From": "+15550000000"}, header, TOKEN)
    assert not verify_signature(VOICE_URL, INCOMING, header, "another-token")
    assert not verify_signature(VOICE_URL, INCOMING, None, TOKEN)


def test_an_incoming_call_verifies_its_attestation_before_the_context(
    on_mock: Niadra, mock_app: MockApp
) -> None:
    seed(on_mock)
    body = urlencode(INCOMING)
    call = parse_call(body, twilio_signed(VOICE_URL, INCOMING), VOICE_URL, TOKEN)
    assert call is not None
    assert (call.call_sid, call.subject, call.level and call.level.value) == (CALL_SID, MARINA, "V2")
    conversation = call.conversation(on_mock)
    assert call.verify(conversation) is not None
    context = conversation.context()
    assert context.verification.effective.value == "V2"
    assert [(v.method, v.level.value) for v in items(mock_app.cell, "verify")] == [
        ("network_attestation", "V2")
    ]
    assert call.voice_info().network_attestation == "A"


def test_a_failed_or_missing_validation_proves_nothing(on_mock: Niadra, mock_app: MockApp) -> None:
    for verstat in ("TN-Validation-Failed-A", "No-TN-Validation", ""):
        params = {**INCOMING, "StirVerstat": verstat}
        call = parse_call(urlencode(params), twilio_signed(VOICE_URL, params), VOICE_URL, TOKEN)
        assert call is not None and call.level is None
        assert call.verify(call.conversation(on_mock)) is None
    assert not items(mock_app.cell, "verify")


def test_an_unsigned_webhook_is_refused() -> None:
    assert parse_call(urlencode(INCOMING), {}, VOICE_URL, TOKEN) is None
    assert parse_call(urlencode(INCOMING), twilio_signed(SMS_URL, INCOMING), VOICE_URL, TOKEN) is None
    assert parse_message(urlencode(WHATSAPP), {"X-Twilio-Signature": "x"}, SMS_URL, TOKEN) is None


def test_the_final_status_callback_ends_the_conversation(on_mock: Niadra, mock_app: MockApp) -> None:
    ringing = parse_call(INCOMING, twilio_signed(VOICE_URL, INCOMING), VOICE_URL, TOKEN)
    assert ringing is not None and not ringing.ended(ringing.conversation(on_mock))
    params = {**INCOMING, "CallStatus": "completed", "CallDuration": "74"}
    done = parse_call(urlencode(params), twilio_signed(VOICE_URL, params), VOICE_URL, TOKEN)
    assert done is not None and done.ended(done.conversation(on_mock))
    on_mock.flush()
    assert CALL_SID in mock_app.cell.ended


def test_an_outbound_call_has_the_customer_in_to() -> None:
    params = {**INCOMING, "Direction": "outbound-api", "From": "+551130001000", "To": "+5511912345678"}
    call = parse_call(urlencode(params), twilio_signed(VOICE_URL, params), VOICE_URL, TOKEN)
    assert call is not None and call.subject == MARINA


def test_a_whatsapp_message_through_twilio_is_the_customers_turn(on_mock: Niadra, mock_app: MockApp) -> None:
    message = parse_message(urlencode(WHATSAPP), twilio_signed(SMS_URL, WHATSAPP), SMS_URL, TOKEN)
    assert message is not None
    assert (message.channel, message.subject, message.profile_name) == (
        "whatsapp",
        whatsapp("5511912345678"),
        "Marina Souza",
    )
    assert message.media == ((WHATSAPP["MediaUrl0"], "image/jpeg"),)
    with on_mock.conversation("wa-thread", subject=message.subject, channel=message.channel) as chat:
        assert message.record(chat)
        assert chat.verification.value == "V1"
        assert message.record(chat)
    on_mock.flush()
    assert turns(mock_app.cell, "wa-thread") == [("customer", "Is my replacement lid on its way?")]
    (event,) = [e for e in events(mock_app.cell, "wa-thread") if e.kind.value == "message"]
    assert event.idempotency_key == WHATSAPP["MessageSid"]
    assert event.verification_hint is not None and event.verification_hint.value == "V1"


async def test_an_sms_through_twilio_with_the_async_client(
    on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    params = {
        "Body": "Call me back",
        "From": "+5511912345678",
        "To": "+551130001000",
        "MessageSid": "SM9",
        "NumMedia": "0",
    }
    message = parse_message(urlencode(params), twilio_signed(SMS_URL, params), SMS_URL, TOKEN)
    assert message is not None and message.channel == "sms" and message.subject == phone("+5511912345678")
    async with on_mock_async.conversation("sms-1", subject=message.subject, channel="sms") as chat:
        assert message.record(chat)
    await on_mock_async.flush()
    assert turns(mock_app.cell, "sms-1") == [("customer", "Call me back")]
