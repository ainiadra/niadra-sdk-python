"""An LCEL chain with the customer's context and the history tools."""

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from niadra import Niadra, phone
from niadra.integrations.langchain import NiadraCallbackHandler, context_runnable, history_tools

niadra = Niadra(channel="chat")
prompt = ChatPromptTemplate.from_messages([("system", "You are Acme's agent."), ("human", "{question}")])

with niadra.conversation("thread-81", subject=phone("+5511912345678")) as conversation:
    model = ChatOpenAI(model="gpt-4.1").bind_tools(history_tools(conversation))
    chain = prompt | context_runnable(conversation) | model
    reply = chain.invoke(
        {"question": "Where is my replacement lid?"},
        config={"callbacks": [NiadraCallbackHandler(conversation)]},
    )
    print(reply.content)
