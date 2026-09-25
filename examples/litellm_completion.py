"""Any provider through LiteLLM with the customer's context."""

from niadra import Niadra, phone
from niadra.integrations.litellm import completion

niadra = Niadra(channel="chat")

with niadra.conversation("thread-81", subject=phone("+5511912345678")) as conversation:
    conversation.customer("Where is my replacement lid?")
    response = completion(
        model="anthropic/claude-sonnet-4-5",
        messages=[
            {"role": "system", "content": "You are Acme's agent."},
            {"role": "user", "content": "Where is my replacement lid?"},
        ],
    )
    print(response.choices[0].message.content)
