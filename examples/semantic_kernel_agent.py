"""A Semantic Kernel ChatCompletionAgent with the customer's memory as a thread, filters and a plugin."""

import asyncio

from semantic_kernel import Kernel
from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai import FunctionChoiceBehavior
from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion

from niadra import AsyncNiadra, phone
from niadra.integrations.semantic_kernel import NiadraKernel

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    memory = NiadraKernel(niadra.conversation("thread-81", subject=phone("+5511912345678")))
    agent = ChatCompletionAgent(
        service=OpenAIChatCompletion(ai_model_id="gpt-4.1"),
        kernel=memory.register(Kernel()),
        name="acme",
        instructions="You are Acme's agent.",
        function_choice_behavior=FunctionChoiceBehavior.Auto(),
    )
    thread = memory.thread()
    response = await agent.get_response(messages="Where is my replacement lid?", thread=thread)
    print(response.content)
    await niadra.close()


asyncio.run(main())
