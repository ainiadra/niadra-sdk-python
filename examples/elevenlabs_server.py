"""The server side of an ElevenLabs phone agent: initiation, tools and post-call webhooks.

Run: uvicorn elevenlabs_server:app. In the ElevenLabs agent, set the initiation webhook to
/elevenlabs/initiation, add the tools from tool_configs(), and the post-call webhook to
/elevenlabs/post-call. Put {{niadra_agent_memory}} and {{niadra_context}} in the system prompt.
"""

import os

from fastapi import FastAPI, Request, Response

from niadra import AsyncNiadra
from niadra.integrations.elevenlabs import ElevenLabsWebhooks, tool_configs

niadra = AsyncNiadra(channel="voice")
hooks = ElevenLabsWebhooks(
    niadra,
    webhook_secret=os.environ["ELEVENLABS_WEBHOOK_SECRET"],
    shared_secret=os.environ["NIADRA_TOOL_SECRET"],
    agent_memory=True,
)
app = FastAPI()
TOOLS = tool_configs("https://agent.example.com/elevenlabs/tools", secret=os.environ["NIADRA_TOOL_SECRET"])


def answer(result) -> Response:
    return Response(result.text(), result.status, media_type=result.content_type)


@app.post("/elevenlabs/initiation")
async def initiation(request: Request) -> Response:
    return answer(await hooks.conversation_initiation(await request.body(), request.headers))


@app.post("/elevenlabs/tools/{name}")
async def tool(name: str, request: Request) -> Response:
    return answer(await hooks.server_tool(name, await request.body(), request.headers))


@app.post("/elevenlabs/post-call")
async def post_call(request: Request) -> Response:
    return answer(await hooks.post_call(await request.body(), request.headers))
