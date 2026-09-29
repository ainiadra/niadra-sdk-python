"""Object state: before the agent states a value of an order, invoice or quote, it asks whether it may.

Niadra answers from what the company's systems pushed, with each field's freshness. When the value is stale
and the company registered a resolver for the type, the SDK reads it fresh from the company's own system,
within the same budget; a stale value is never verified.

Needs the space's `state` feature and the type `health_quote` declared.
"""

from __future__ import annotations

from collections.abc import Sequence

from niadra import Conversation, Niadra, phone
from niadra.models.state import StateRef
from niadra.resolvers import Resolved

QUOTE = "health_quote:op:q-77"
QUOTES = {"q-77": {"price_full": 499.9, "plan": "ouro"}}


def requote(ref: StateRef, fields: Sequence[str] | None) -> Resolved:
    """The company's own read of a quote, the `fields` asked (all when None): here a fixed table, in
    production its quoting system."""
    quote = QUOTES[ref.id]
    return Resolved({k: v for k, v in quote.items() if fields is None or k in fields}, version=7)


def price_line(conversation: Conversation, price: float) -> str:
    """What the agent says about the quote's full price: the price only when it may be claimed now."""
    verdict = conversation.verify_claim(QUOTE, "price_full", price)
    if verdict.claim_safe:
        return f"O plano sai por R$ {price:.2f}."
    if verdict.value is not None:
        return f"O plano sai por R$ {float(verdict.value):.2f}."
    return "Vou confirmar o valor atualizado e já te digo."


if __name__ == "__main__":
    niadra = Niadra(channel="whatsapp")
    niadra.resolvers.register("health_quote", requote)
    customer = phone("+5511912345678")
    with (
        niadra.conversation("thread-84", subject=customer, agent_id="sales") as conversation,
        conversation.turn(),
    ):
        print(price_line(conversation, 511.06))
    niadra.close()
