"""A Google ADK agent with the customer's memory in its model callbacks."""

import asyncio

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from niadra import AsyncNiadra, phone
from niadra.integrations.google_adk import NiadraADK

niadra = AsyncNiadra(channel="chat")


async def main() -> None:
    memory = NiadraADK(niadra.conversation("thread-81", subject=phone("+5511912345678")))
    agent = LlmAgent(
        name="support",
        model="gemini-2.5-flash",
        instruction="You are Acme's agent.",
        tools=memory.tools,
        before_model_callback=memory.before_model,
        after_model_callback=memory.after_model,
    )
    runner = InMemoryRunner(agent=agent, app_name="acme")
    session = await runner.session_service.create_session(app_name="acme", user_id="marina")
    message = types.Content(role="user", parts=[types.Part(text="Where is my replacement lid?")])
    async for event in runner.run_async(user_id="marina", session_id=session.id, new_message=message):
        if event.is_final_response() and event.content:
            print(event.content.parts[0].text)
    await niadra.close()


asyncio.run(main())
