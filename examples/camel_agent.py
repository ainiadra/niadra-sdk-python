"""A CAMEL ChatAgent with the customer's memory as its agent memory and the history tools."""

from camel.agents import ChatAgent
from camel.models import ModelFactory
from camel.types import ModelPlatformType, ModelType

from niadra import Niadra, phone
from niadra.integrations.camel import NiadraMemory

niadra = Niadra(channel="chat")
memory = NiadraMemory(niadra.conversation("thread-81", subject=phone("+5511912345678")))
model = ModelFactory.create(model_platform=ModelPlatformType.OPENAI, model_type=ModelType.GPT_4_1)
agent = memory.attach(ChatAgent(system_message="You are Acme's agent.", model=model, tools=memory.tools))
print(agent.step("Where is my replacement lid?").msgs[0].content)
niadra.close()
