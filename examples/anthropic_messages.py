"""Anthropic's Messages API with the customer's context in the system prompt."""

from anthropic import Anthropic

from niadra import Niadra, phone
from niadra.integrations.anthropic import wrap

niadra = Niadra(channel="chat")
claude = wrap(Anthropic())

with niadra.conversation("thread-81", subject=phone("+5511912345678")) as conversation:
    conversation.customer("Where is my replacement lid?")
    message = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=512,
        system=[{"type": "text", "text": "You are Acme's agent.", "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "Where is my replacement lid?"}],
    )
    print(message.content[0].text)
