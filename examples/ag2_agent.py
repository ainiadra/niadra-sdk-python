"""An AG2 agent with the customer's memory as middleware and tools."""

import asyncio

from ag2 import Agent
from ag2.config import OpenAIConfig

from niadra import AsyncNiadra, phone
from niadra.integrations.ag2 import NiadraAG2

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    memory = NiadraAG2(niadra.conversation("thread-81", subject=phone("+5511912345678")))
    agent = Agent(
        "acme",
        "You are Acme's agent.",
        config=OpenAIConfig(model="gpt-4.1"),
        tools=memory.tools,
        middleware=[memory.middleware],
    )
    reply = await agent.ask("Where is my replacement lid?")
    print(reply.body)
    await niadra.close()


asyncio.run(main())
