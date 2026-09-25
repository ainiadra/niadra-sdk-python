"""An Agno agent with the customer's memory as instructions, tools and hooks."""

import asyncio

from agno.agent import Agent
from agno.models.openai import OpenAIChat

from niadra import AsyncNiadra, phone
from niadra.integrations.agno import NiadraAgno

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    memory = NiadraAgno(
        niadra.conversation("thread-81", subject=phone("+5511912345678")),
        instructions="You are Acme's agent.",
    )
    agent = Agent(
        model=OpenAIChat(id="gpt-4.1"),
        instructions=memory.instructions,
        tools=memory.tools,
        pre_hooks=[memory.pre_hook],
        post_hooks=[memory.post_hook],
    )
    print((await agent.arun("Where is my replacement lid?")).content)
    await niadra.close()


asyncio.run(main())
