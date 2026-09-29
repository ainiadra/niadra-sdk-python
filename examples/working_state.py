"""The agent's working state: a small state its code keeps between turns, written by code, never by a model.

Two sub-agents of one conversation write different fields with `merge_by_key` and never lose each other's
writes. A read after a write serves at least what was written. With Niadra down, the write applies to the
local copy at once and leaves later.

Needs the space's `agent_state` feature.
"""

from __future__ import annotations

from typing import Any

from niadra import Conversation, Niadra, phone


def remember_offer(conversation: Conversation, status: str) -> dict[str, Any]:
    """The offer sub-agent's field."""
    conversation.agent_state.put({"offer": {"status": status}}, mode="merge_by_key")
    return conversation.agent_state.get().body


def remember_address(conversation: Conversation, city: str) -> dict[str, Any]:
    """The delivery sub-agent's field, written without reading the offer first."""
    conversation.agent_state.put({"delivery": {"city": city}}, mode="merge_by_key")
    return conversation.agent_state.get().body


if __name__ == "__main__":
    niadra = Niadra(channel="whatsapp")
    with niadra.conversation("thread-85", subject=phone("+5511912345678"), agent_id="sales") as conversation:
        remember_offer(conversation, "accepted")
        print(remember_address(conversation, "Campinas"))
    niadra.close()
