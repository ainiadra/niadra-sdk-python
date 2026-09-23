"""Business objects on the read side: `GET /v1/objects/{type}/{namespace}/{id}` and `/timeline`."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from niadra.models._base import ResponseModel
from niadra.models.common import ObjectRef
from niadra.models.context import HistoryItem


class ObjectTimeline(ResponseModel):
    """System events and agent actions about one object, newest first; never conversation content."""

    ref: ObjectRef
    items: list[HistoryItem] = Field(default_factory=list)
    next_cursor: str | None = None
    as_of: datetime | None = None
