"""A Twilio voice webhook that verifies the carrier's attestation before the first context."""

import os

from flask import Flask, request

from niadra import Niadra
from niadra.integrations.twilio import parse_call

niadra = Niadra(channel="voice")
app = Flask(__name__)


@app.post("/twilio/voice")
def incoming_call() -> tuple[str, int]:
    call = parse_call(request.get_data(), request.headers, request.url, os.environ["TWILIO_AUTH_TOKEN"])
    if call is None:
        return "", 403
    conversation = call.conversation(niadra)
    call.verify(conversation)  # StirVerstat: A proves V2, B and C prove V1
    context = conversation.context()
    return connect_your_voice_agent(call.call_sid, context.system_block), 200


@app.post("/twilio/status")
def status() -> tuple[str, int]:
    call = parse_call(request.get_data(), request.headers, request.url, os.environ["TWILIO_AUTH_TOKEN"])
    if call is not None:
        call.ended(call.conversation(niadra))
    return "", 204


def connect_your_voice_agent(call_sid: str, context: str) -> str:
    raise NotImplementedError("return the TwiML that connects the call to your agent")
