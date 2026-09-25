"""Azure OpenAI through the same wrap() as OpenAI."""

from openai import AzureOpenAI

from niadra import Niadra, phone, wrap

niadra = Niadra(channel="chat")
azure = wrap(AzureOpenAI(api_version="2025-04-01-preview"))  # AZURE_OPENAI_ENDPOINT and _API_KEY

with niadra.conversation("thread-81", subject=phone("+5511912345678")) as conversation:
    conversation.customer("Where is my replacement lid?")
    reply = azure.chat.completions.create(
        model="support-gpt41",  # your deployment name
        messages=[
            {"role": "system", "content": "You are Acme's agent."},
            {"role": "user", "content": "Where is my replacement lid?"},
        ],
    )
    print(reply.choices[0].message.content)
