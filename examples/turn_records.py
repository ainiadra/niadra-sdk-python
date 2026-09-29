"""A store agent whose turns are recorded, and which keeps working with Niadra out of reach.

Each answer is a turn: its read (with the customer's constraints block), its tool call, the model behind it
and what it said go to the turn record, and the claim contract checks the prices it states in count mode,
changing nothing. A follow-up offer goes only when the customer did not opt out of marketing.

With Niadra down, the read serves the last good pack and constraints (`degraded`), the claims are checked
against the contract already in the local cache, turns wait in the queue, and the opt-out holds from the
last copy of the suppression list: nothing the agent does waits for Niadra.

Needs the space's `turns` feature, `signals` for the constraints block and `coordination` for the
suppression list, and an OpenAI key for the model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from niadra import Conversation, Handle, Niadra, phone

INSTRUCTIONS = "You are the store's agent. Quote prices exactly as the product data gives them."
CATALOG = {"PX-4471": {"sku": "PX-4471", "name": "Running shoe PX", "price_sale": 199.9, "price_list": 299.9}}


def _shown(product: dict[str, Any]) -> list[dict[str, Any]]:
    fields = {"price_sale": product["price_sale"], "price_list": product["price_list"]}
    return [{"ref": f"product:store:{product['sku']}", "fields": fields}]


@Niadra.tool("search_products", provenance=_shown)
def search_products(sku: str) -> dict[str, Any]:
    return CATALOG[sku]


def answer(conversation: Conversation, text: str, model: Callable[[str], str]) -> str:
    """One turn: the customer's message in, the agent's reply out, recorded on the way."""
    conversation.customer(text)
    with conversation.turn(build=Niadra.build(prompts={"store": "v3"}, model="gpt-4.1-mini")):
        context = conversation.context(include=["constraints"])
        product = search_products("PX-4471")
        prompt = f"{INSTRUCTIONS}\n\n{context.system_block}\n\nProduct: {product}\n\n{context.turn_block}"
        conversation.mark_injected(context)
        reply = model(prompt)
        conversation.agent(reply)
    return reply


def offer_follow_up(niadra: Niadra, conversation: Conversation, customer: Handle) -> bool:
    """A marketing message the agent starts: it goes only if the customer did not opt out."""
    if not niadra.may_contact(customer, "marketing", channel="whatsapp"):
        return False
    conversation.agent("The PX comes in two new colors this week. Want to see them?")
    return True


if __name__ == "__main__":
    from openai import OpenAI

    openai = OpenAI()

    def model(prompt: str) -> str:
        completion = openai.chat.completions.create(
            model="gpt-4.1-mini", messages=[{"role": "system", "content": prompt}]
        )
        return completion.choices[0].message.content or ""

    niadra = Niadra(channel="whatsapp")
    customer = phone("+5511912345678")
    with niadra.conversation("thread-81", subject=customer, agent_id="store") as conversation:
        print(answer(conversation, "How much is the PX-4471 running shoe?", model))
        print("offer sent" if offer_follow_up(niadra, conversation, customer) else "the customer opted out")
    niadra.close()
