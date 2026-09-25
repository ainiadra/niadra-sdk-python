"""A LlamaIndex FunctionAgent with the customer's memory and the history tools."""

import asyncio

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.llms.openai import OpenAI

from niadra import AsyncNiadra, phone
from niadra.integrations.llamaindex import NiadraMemory, history_tools

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    conversation = niadra.conversation("thread-81", subject=phone("+5511912345678"))
    agent = FunctionAgent(
        llm=OpenAI(model="gpt-4.1"), system_prompt="You are Acme's agent.", tools=history_tools(conversation)
    )
    print(await agent.run("Where is my replacement lid?", memory=NiadraMemory(conversation)))
    await niadra.close()


asyncio.run(main())
