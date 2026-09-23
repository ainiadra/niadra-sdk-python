"""Building blocks shared by the write and read sides of the API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field, StringConstraints

from niadra.models._base import IdStr, Model, ResponseModel, ShortStr
from niadra.vocabulary import HandleType, SubjectKind


class Handle(Model):
    """An identifier of a subject in some channel or system: a phone, an e-mail, a CRM id.

    Build them with the helpers in `niadra.handles` (`phone()`, `email()`, `system_id()`...),
    which normalize the value the way the server expects.
    """

    type: HandleType
    value: Annotated[str, StringConstraints(min_length=1, max_length=320)]
    scope: ShortStr | None = Field(
        default=None,
        description="Namespace for scoped identifiers: the WhatsApp Business account for `wa_bsuid`,"
        " the system for `system_id`, the country for `gov_id_hmac`.",
    )
    subject_kind: SubjectKind | None = Field(
        default=None, description="Defaults to `person`, except for organization-only handle types."
    )


class ObjectRef(Model):
    """A business object in a system of record, e.g. `invoice` / `erp` / `0823`."""

    type: ShortStr
    namespace: ShortStr
    id: IdStr

    @classmethod
    def parse(cls, value: str) -> ObjectRef:
        """Parses the `type:namespace:id` shorthand. The id may itself contain colons."""
        parts = value.split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("an object reference looks like `type:namespace:id`")
        return cls(type=parts[0], namespace=parts[1], id=parts[2])


class Subject(Model):
    kind: SubjectKind
    role: ShortStr | None = None
    handles: list[Handle] = Field(min_length=1, max_length=16)


class SourceCoverage(ResponseModel):
    source_id: str
    status: str = Field(description="`ok`, or `silent` when the source stopped sending.")
    last_event_at: datetime | None = None


class Problem(ResponseModel):
    """RFC 9457 problem details; `code` comes from the versioned error catalog."""

    type: str = "about:blank"
    title: str = ""
    status: int = 0
    detail: str | None = None
    code: str = "unknown"
    request_id: str | None = None
