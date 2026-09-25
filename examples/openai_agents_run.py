"""An OpenAI Agents SDK agent with the customer's memory."""

import asyncio

from agents import Agent, Runner

from niadra import AsyncNiadra, phone
from niadra.integrations.openai_agents import NiadraAgentsMemory

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    async with niadra.conversation("thread-81", subject=phone("+5511912345678")) as conversation:
        memory = NiadraAgentsMemory(conversation, agent_memory=True)
        agent = Agent(
            name="Support", instructions="You are Acme's agent.", model="gpt-4.1", tools=memory.tools
        )
        result = await Runner.run(
            agent, "Where is my replacement lid?", hooks=memory.hooks, run_config=memory.run_config()
        )
        print(result.final_output)
    await niadra.close()


asyncio.run(main())
