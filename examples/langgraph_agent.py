"""A LangGraph agent (langchain.agents.create_agent) with the customer's memory as middleware."""

import asyncio

from langchain.agents import create_agent

from niadra import AsyncNiadra, phone
from niadra.integrations.langgraph import NiadraMiddleware

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    async with niadra.conversation("thread-81", subject=phone("+5511912345678")) as conversation:
        agent = create_agent(
            "openai:gpt-4.1",
            system_prompt="You are Acme's agent.",
            middleware=[NiadraMiddleware(conversation)],
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Where is my replacement lid?"}]}
        )
        print(result["messages"][-1].content)
    await niadra.close()


asyncio.run(main())
