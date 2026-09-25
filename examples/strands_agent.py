"""A Strands agent with the customer's memory as hooks."""

import asyncio

from strands import Agent

from niadra import AsyncNiadra, phone
from niadra.integrations.strands import NiadraHooks

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    memory = NiadraHooks(niadra.conversation("thread-81", subject=phone("+5511912345678")))
    agent = Agent(system_prompt="You are Acme's agent.", tools=memory.tools, hooks=[memory])
    print(await agent.invoke_async("Where is my replacement lid?"))
    await niadra.close()


asyncio.run(main())
