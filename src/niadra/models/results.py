"""What the client methods return: the wire responses plus what the SDK knows about the call."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from niadra.constraints.text import include_text
from niadra.models._base import ResponseModel
from niadra.models.context import (
    ContextResponse,
    LiveTurn,
    SearchResponse,
    TimelineResponse,
    VerificationResult,
)
from niadra.vocabulary import DeliveryPath, Verification

Origin = Literal["network", "cache", "stale", "last_good", "empty"]


class Context(ContextResponse):
    """The context pack for one subject, ready to put in front of a model.

    `origin` says where this value came from:

    - `network`: a fresh answer from the API.
    - `cache`: the conversation cache, still within its TTL (or revalidated with an ETag).
    - `stale`: past the TTL but within the stale window; a refresh is running in the background.
    - `last_good`: the API failed and this is the last pack that worked for the same key, `degraded`.
    - `empty`: nothing to serve. `error` says why, unless the subject is simply in a holdout group.

    `age_ms` is how long ago Niadra sent or confirmed the pack served: 0 for an answer just received, growing
    while a cached pack is served, and None when there is no pack.

    An empty pack is still a valid answer: inject nothing and carry on.
    """

    version: str = ""
    etag: str = ""
    verification: VerificationResult = Field(
        default_factory=lambda: VerificationResult(requested=Verification.V0, effective=Verification.V0)
    )
    path: DeliveryPath | str = DeliveryPath.T4
    origin: Origin = "network"
    error: str | None = None
    elapsed_ms: float | None = None
    age_ms: float | None = None
    asked_blocks: list[str] = Field(default_factory=list)
    """The blocks the read asked by `include` (after any the space does not serve)."""

    @classmethod
    def empty(cls, *, requested: Verification = Verification.V0, error: str | None = None) -> Context:
        effective = Verification.NO_CUSTOMER if requested is Verification.NO_CUSTOMER else Verification.V0
        return cls(
            verification=VerificationResult(requested=requested, effective=effective),
            degraded=error is not None,
            origin="empty",
            error=error,
        )

    def __bool__(self) -> bool:
        return bool(self.text)

    @property
    def is_holdout(self) -> bool:
        """The subject is in a control group. The pack is empty on purpose; this is not an error."""
        return self.path == DeliveryPath.HOLDOUT

    @property
    def unread_blocks(self) -> list[str]:
        """Blocks the read asked by `include` that the answer does not carry: the server could not read them
        this time and said `degraded`. A missing `constraints` block is said in the turn block, never left
        silent: a model that is not told a restriction may hold would answer as if none did."""
        return [name for name in self.asked_blocks if getattr(self, name, None) is None]

    @property
    def system_block(self) -> str:
        """The pinned pack, to place near the start of the prompt. Empty string when there is none."""
        return self.text or ""

    @property
    def turn_block(self) -> str:
        """What changes every turn, for the end of the prompt: the recent turns from other
        channels, this turn's slots (what the customer's last turn selected from memory), the
        blocks the read asked for by `include` (the state view, the constraints) and the delta, in
        that order, as the API places them.

        None of them is part of the pinned pack: they go after the conversation, where they do
        not break the cached prefix. Empty string when there is none.
        """
        return render_turn(self)


def render_turn(context: ContextResponse) -> str:
    """The turn block of an answer: the live turns, then the slots, then the blocks by `include`, then the
    delta. Empty for a holdout."""
    if context.path == DeliveryPath.HOLDOUT:
        return ""
    unread = context.unread_blocks if isinstance(context, Context) else ()
    included = include_text(context.text, context.state, context.constraints, unread=unread)
    return "\n\n".join(
        part for part in (render_live(context), context.slots or "", included, context.delta or "") if part
    )


def render_live(context: ContextResponse) -> str:
    """The live turns as one tagged block, or an empty string when there are none.

    `complete="false"` tells the model the list may be missing turns the server could not read.
    """
    if not context.live:
        return ""
    complete = "" if context.live_complete else ' complete="false"'
    lines = "\n".join(_live_line(turn) for turn in context.live)
    return f'<live_turns source="niadra"{complete}>\n{lines}\n</live_turns>'


def _live_line(turn: LiveTurn) -> str:
    at = turn.at if turn.at.tzinfo else turn.at.replace(tzinfo=timezone.utc)
    return f"[{at.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] {turn.channel} · {turn.speaker}: {turn.text}"


class SearchResult(SearchResponse):
    """History search results. On failure, `items` is empty and `error` says why."""

    error: str | None = None


class TimelinePage(TimelineResponse):
    """One page of the subject's history, most recent first. On failure, `error` says why."""

    error: str | None = None


class MediaUpload(ResponseModel):
    """A file handed to Niadra. Put `media_ref` and `media_sha256` in the event's `content`."""

    media_ref: str
    media_sha256: str
    content_type: str
    size_bytes: int
    expires_at: datetime | None = None
