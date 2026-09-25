"""A Pydantic AI agent with the customer's memory as a capability."""

import asyncio

from pydantic_ai import Agent

from niadra import AsyncNiadra, phone
from niadra.integrations.pydantic_ai import NiadraCapability

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    capability = NiadraCapability(niadra.conversation("thread-81", subject=phone("+5511912345678")))
    agent = Agent("openai:gpt-4.1", instructions="You are Acme's agent.", capabilities=[capability])
    print((await agent.run("Where is my replacement lid?")).output)
    await niadra.close()


asyncio.run(main())
