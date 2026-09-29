"""Coordination: ask before acting, so two agents never do the same thing to one customer.

The closing agent's farewell is an effect with a key: the first check reserves it, the agent sends it and
declares it done, and every later check of the same key hears it is done. A marketing message goes only
when the customer did not opt out. With Niadra down, each purpose decides its own way (a farewell waits, a
service answer goes).

Needs the space's `coordination` feature.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from niadra import Conversation, Niadra, phone


def farewell_once(conversation: Conversation, send: Callable[[str], Any]) -> bool:
    """Sends the farewell of this conversation once, whichever agent tries it first."""
    key = f"farewell:{conversation.id}"
    decision = conversation.check("farewell", purpose="service", effect_key=key)
    if decision.decision != "allow":
        return False
    send("Obrigada pelo contato! Qualquer coisa, é só chamar.")
    conversation.declare.effect(key, "done")
    return True


if __name__ == "__main__":
    niadra = Niadra(channel="whatsapp")
    customer = phone("+5511912345678")
    with (
        niadra.conversation("thread-83", subject=customer, agent_id="closing") as conversation,
        conversation.turn(),
    ):
        print("sent" if farewell_once(conversation, conversation.agent) else "another agent already said it")
    print("may contact" if niadra.may_contact(customer, "marketing", channel="whatsapp") else "opted out")
    niadra.close()
