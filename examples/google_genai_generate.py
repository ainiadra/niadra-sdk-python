"""Gemini through the Google GenAI SDK with the customer's context."""

from google import genai

from niadra import Niadra, phone
from niadra.integrations.google_genai import wrap

niadra = Niadra(channel="chat")
gemini = wrap(genai.Client())

with niadra.conversation("thread-81", subject=phone("+5511912345678")) as conversation:
    conversation.customer("Where is my replacement lid?")
    response = gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents="Where is my replacement lid?",
        config={"system_instruction": "You are Acme's agent."},
    )
    print(response.text)
