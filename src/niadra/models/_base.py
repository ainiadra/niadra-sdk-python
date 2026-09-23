from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

ShortStr = Annotated[str, StringConstraints(min_length=1, max_length=256)]
IdStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]


class Model(BaseModel):
    """Anything the SDK sends. Unknown fields are rejected, as the server does, so typos fail early."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ResponseModel(BaseModel):
    """Anything the SDK receives. Unknown fields are ignored: `/v1` may gain fields at any time."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)
