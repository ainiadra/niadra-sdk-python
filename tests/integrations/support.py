"""What the adapter tests share: a customer with some history on another channel, and readers of
what reached the emulator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from niadra import AsyncNiadra, Niadra, phone
from niadra.models.events import EventItem
from niadra.tools import BUILTIN_DEFINITIONS
from niadra_mock import MockCell

MARINA = phone("+5511912345678")
EARLIER = "The lid of order 4471 arrived broken, I need a new one by Friday"

# The kit's tools as every adapter must hand them to its framework, word for word.
DEFINITIONS = {d["function"]["name"]: d["function"] for d in BUILTIN_DEFINITIONS}


def _earlier_turn() -> dict[str, Any]:
    return {
        "channel": "whatsapp",
        "conversation_id": "wa-earlier",
        "handles": [MARINA],
        "speaker": {"role": "customer"},
        "content": {"text": EARLIER},
        "occurred_at": datetime.now(timezone.utc) - timedelta(minutes=30),
    }


def seed(niadra: Niadra) -> None:
    niadra.track(_earlier_turn())
    niadra.flush()


async def seed_async(niadra: AsyncNiadra) -> None:
    niadra.track(_earlier_turn())
    await niadra.flush()


def turns(cell: MockCell, conversation_id: str) -> list[tuple[str, str]]:
    """`(speaker role, text)` of each message the conversation recorded, in order."""
    return [
        (e.item.speaker.role.value, e.text)
        for e in cell.events
        if e.item.conversation_id == conversation_id and e.item.kind.value == "message"
    ]


def events(cell: MockCell, conversation_id: str) -> list[EventItem]:
    return [e.item for e in cell.events if e.item.conversation_id == conversation_id]


def items(cell: MockCell, kind: str) -> list[Any]:
    """Batch items of one `type` (verify, handoff, conversation.ended...) that reached the cell."""
    return [i for i in cell.items if getattr(i, "type", None) == kind]
