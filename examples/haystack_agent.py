"""A Haystack Agent with the customer's memory as hooks and tools."""

from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage

from niadra import Niadra, phone
from niadra.integrations.haystack import NiadraAgentHooks, history_tools

niadra = Niadra(channel="chat")
conversation = niadra.conversation("thread-81", subject=phone("+5511912345678"))
agent = Agent(
    chat_generator=OpenAIChatGenerator(model="gpt-4.1"),
    system_prompt="You are Acme's agent.",
    tools=history_tools(conversation),
    hooks=NiadraAgentHooks(conversation).hooks,
)
result = agent.run(messages=[ChatMessage.from_user("Where is my replacement lid?")])
print(result["last_message"].text)
niadra.close()
