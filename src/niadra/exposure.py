"""The exposure token (`spec/exposure-token.md`): `nx1.<id>.<position>.<verifier>`, the short string a card
carries so that the order line the store's app copies it into names the list the agent showed and the card's
place in it.

The token carries no personal data. Its verifier catches a token copied wrong or cut short; it is not a
signature.
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from typing import Literal

from niadra.errors import NiadraError

_ID = re.compile(r"[A-Za-z0-9_-]{22}")
_VERIFIER = re.compile(r"[A-Za-z0-9_-]{4}")
_POSITION = re.compile(r"[1-9][0-9]{0,3}")

Refusal = Literal["malformed", "unsupported_version", "bad_position", "bad_verifier"]


class ExposureTokenError(NiadraError, ValueError):
    """A token a reader refuses, with the code of the first check it fails (`spec/exposure-token.md`,
    section 4)."""

    def __init__(self, code: Refusal) -> None:
        super().__init__(code)
        self.code = code


def exposure_token(exposure_id: str | uuid.UUID, position: int) -> str:
    """The token of the card at `position` (1-based, up to 9999) of the exposure `exposure_id`: 33 characters
    for the first card, at most 36."""
    if type(position) is not int or not 1 <= position <= 9999:
        raise ValueError("a card's position is from 1 to 9999")
    raw = exposure_id if isinstance(exposure_id, uuid.UUID) else uuid.UUID(exposure_id)
    head = f"nx1.{_b64(raw.bytes)}.{position}"
    return f"{head}.{_verifier(head)}"


def parse_exposure_token(token: str) -> tuple[str, int]:
    """The exposure id, written as a UUID, and the position a token names. Raises `ExposureTokenError`."""
    segments = token.split(".")
    if len(segments) != 4:
        raise ExposureTokenError("malformed")
    version, ident, position, verifier = segments
    if version != "nx1":
        raise ExposureTokenError("unsupported_version")
    if not _ID.fullmatch(ident):
        raise ExposureTokenError("malformed")
    raw = base64.urlsafe_b64decode(ident + "==")
    if _b64(raw) != ident:
        raise ExposureTokenError("malformed")
    if not _VERIFIER.fullmatch(verifier):
        raise ExposureTokenError("malformed")
    if not _POSITION.fullmatch(position):
        raise ExposureTokenError("bad_position")
    if _verifier(f"{version}.{ident}.{position}") != verifier:
        raise ExposureTokenError("bad_verifier")
    return str(uuid.UUID(bytes=raw)), int(position)


def _verifier(head: str) -> str:
    return _b64(hashlib.sha256(head.encode("ascii")).digest())[:4]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
