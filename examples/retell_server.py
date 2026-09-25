"""The server side of a Retell voice agent: inbound webhook, custom functions and call events.

Run: uvicorn retell_server:app. On the Retell phone number, set the inbound webhook to
/retell/inbound; add the tools from tool_configs() to the Retell LLM's general_tools; set the
agent's webhook to /retell/events. Put {{niadra_agent_memory}} and {{niadra_context}} in the
prompt, after your instructions.
"""

import os

from fastapi import FastAPI, Request, Response

from niadra import AsyncNiadra
from niadra.integrations.retell import RetellWebhooks, tool_configs

niadra = AsyncNiadra(channel="voice")
retell = RetellWebhooks(niadra, api_key=os.environ["RETELL_API_KEY"], agent_memory=True)
app = FastAPI()
TOOLS = tool_configs("https://agent.example.com/retell/tools", agent_memory=True)


def answer(result) -> Response:
    return Response(result.text(), result.status, media_type=result.content_type)


@app.post("/retell/inbound")
async def inbound(request: Request) -> Response:
    return answer(await retell.inbound(await request.body(), request.headers))


@app.post("/retell/tools")
async def tools(request: Request) -> Response:
    return answer(await retell.custom_function(await request.body(), request.headers))


@app.post("/retell/events")
async def events(request: Request) -> Response:
    return answer(await retell.webhook(await request.body(), request.headers))


async def call_out(to_number: str) -> dict:
    """What to pass to Retell's create_phone_call for an outbound call to a customer."""
    return await retell.outbound(to_number)
