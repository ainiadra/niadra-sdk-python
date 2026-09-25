"""A WhatsApp Cloud API webhook: each message is the customer's turn, each reply the agent's."""

import os

from fastapi import FastAPI, Request, Response

from niadra import Niadra
from niadra.integrations.whatsapp import parse_webhook, sent, subscribe

niadra = Niadra(channel="whatsapp")
app = FastAPI()


@app.get("/whatsapp")
def challenge(request: Request) -> Response:
    result = subscribe(request.query_params, os.environ["WHATSAPP_VERIFY_TOKEN"])
    return Response(result.text(), result.status, media_type=result.content_type)


@app.post("/whatsapp")
async def inbound(request: Request) -> Response:
    messages = parse_webhook(await request.body(), request.headers, os.environ["META_APP_SECRET"])
    if messages is None:
        return Response(status_code=401)
    for message in messages:
        with niadra.conversation(f"wa-{message.wa_id}", subject=message.subject) as chat:
            message.record(chat)
            niadra.flush()
            context = chat.context()
            reply = your_model(context.system_block, context.turn_block, message.text)
            sent(chat, reply, send_whatsapp(message.wa_id, reply))
    return Response(status_code=200)


def your_model(context: str, news: str, text: str | None) -> str:
    raise NotImplementedError("call your model here")


def send_whatsapp(wa_id: str | None, text: str) -> dict:
    raise NotImplementedError("POST /{phone_number_id}/messages on the Graph API and return its JSON")
