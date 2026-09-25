"""A Microsoft Agent Framework agent with the customer's memory as a context provider."""

import asyncio

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient

from niadra import AsyncNiadra, phone
from niadra.integrations.agent_framework import NiadraContextProvider

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    provider = NiadraContextProvider(niadra.conversation("thread-81", subject=phone("+5511912345678")))
    agent = Agent(
        client=OpenAIChatClient(), instructions="You are Acme's agent.", context_providers=[provider]
    )
    print((await agent.run("Where is my replacement lid?")).text)
    await niadra.close()


asyncio.run(main())
