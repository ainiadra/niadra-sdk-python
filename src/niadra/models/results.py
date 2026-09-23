"""What the client methods return: the wire responses plus what the SDK knows about the call."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

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
    - `last_good`: the API failed and this is the last pack that worked for the same key.
    - `empty`: nothing to serve. `error` says why, unless the subject is simply in a holdout group.

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
    def system_block(self) -> str:
        """The pinned pack, to place near the start of the prompt. Empty string when there is none."""
        return self.text or ""

    @property
    def turn_block(self) -> str:
        """The delta and the recent turns from other channels, for the end of the prompt.

        Neither is part of the pinned pack: they change every turn, so they go after the
        conversation, where they do not break the cached prefix. Empty string when there is none.
        """
        if self.is_holdout:
            return ""
        return "\n\n".join(part for part in (self.delta or "", render_live(self)) if part)


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
