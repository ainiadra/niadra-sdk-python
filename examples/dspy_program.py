"""A DSPy ReAct program with the customer's memory in its adapter and the history tools."""

import dspy

from niadra import Niadra, phone
from niadra.integrations.dspy import NiadraModule, history_tools

niadra = Niadra(channel="chat")
conversation = niadra.conversation("thread-81", subject=phone("+5511912345678"))
dspy.configure(lm=dspy.LM("openai/gpt-4.1"), track_usage=True)
react = dspy.ReAct("question -> answer", tools=history_tools(conversation))
agent = NiadraModule(react, conversation, input_field="question", output_field="answer")
print(agent(question="Where is my replacement lid?").answer)
niadra.close()
