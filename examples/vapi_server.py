"""The server URL of a Vapi assistant. Run: uvicorn vapi_server:app"""

import os

from fastapi import FastAPI, Request, Response

from niadra import AsyncNiadra
from niadra.integrations.vapi import VapiServer, tool_definitions

niadra = AsyncNiadra(channel="voice")
vapi = VapiServer(
    niadra, secret=os.environ["VAPI_SERVER_SECRET"], assistant_id=os.environ["VAPI_ASSISTANT_ID"]
)
app = FastAPI()
TOOLS = tool_definitions("https://agent.example.com/vapi")  # add them to the assistant's model.tools


@app.post("/vapi")
async def server(request: Request) -> Response:
    result = await vapi.handle(await request.body(), request.headers)
    return Response(result.text(), result.status, media_type=result.content_type)
