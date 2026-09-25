"""Amazon Bedrock's Converse API with the customer's context."""

import boto3

from niadra import Niadra, phone
from niadra.integrations.bedrock import wrap

niadra = Niadra(channel="chat")
bedrock = wrap(boto3.client("bedrock-runtime"))

with niadra.conversation("thread-81", subject=phone("+5511912345678")) as conversation:
    conversation.customer("Where is my replacement lid?")
    response = bedrock.converse(
        modelId="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        system=[{"text": "You are Acme's agent."}],
        messages=[{"role": "user", "content": [{"text": "Where is my replacement lid?"}]}],
    )
    print(response["output"]["message"]["content"][0]["text"])
