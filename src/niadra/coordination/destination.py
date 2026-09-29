"""The canonical destination of a handle and its key per reader (`spec/suppression-list.md`, sections 3
and 4).

A phone number, a WhatsApp id and an e-mail address each have one canonical form, `phone:+<E.164>` or
`email:<address>`. The suppression list keys it with each reader's salt, and the contact token carries it
keyed with the gateway's key as `rcpt` (`spec/contact-token.md`, section 4.1), so a reader matches the
destination it is about to contact without the list ever carrying a handle.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Literal

from niadra.errors import NiadraError

_SEPARATORS = re.compile(r"[\s().\-/]")
_DIGITS = re.compile(r"[0-9]+")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]*")
_DOMAIN = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]{1,63})+"
_EMAIL = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@" + _DOMAIN + "$")


class DestinationError(NiadraError, ValueError):
    """A handle with no canonical destination: `invalid_handle` when the value is not a handle of its type,
    `unsupported_type` when the list does not cover the type."""

    def __init__(self, code: Literal["invalid_handle", "unsupported_type"]) -> None:
        super().__init__(code)
        self.code = code


def canonical_destination(type: str, value: str) -> str:
    """The canonical form of a handle of `type` (`phone_e164`, `wa_id`, `wa_jid` or `email`), e.g.
    `phone:+5511987654321` for `"(11) 98765-4321"`. Raises `DestinationError`."""
    if type == "phone_e164":
        return _phone(value)
    if type == "wa_id":
        text = value.strip()
        return _phone(text if text.startswith("+") else "+" + text)
    if type == "wa_jid":
        user = value.strip().lower().partition("@")[0].split(":", 1)[0]
        return _phone("+" + user)
    if type == "email":
        address = value.strip().lower()
        if not _EMAIL.match(address):
            raise DestinationError("invalid_handle")
        return "email:" + address
    raise DestinationError("unsupported_type")


def suppression_key(salt_b64url: str, canonical: str) -> str:
    """A canonical destination keyed with a reader's salt, in base64url as `GET /v1/suppressions/salt` gives
    it: the base64url of HMAC-SHA256 over its UTF-8 bytes, 43 characters."""
    mac = hmac.new(_unb64(salt_b64url), canonical.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def _phone(value: str) -> str:
    """`phone:+<digits>` in E.164: a Brazilian national number gains `55`, and a Brazilian mobile number
    from before the ninth digit gains its `9`."""
    raw = _SEPARATORS.sub("", value.strip())
    international = raw.startswith(("+", "00"))
    digits = raw[1:] if raw.startswith("+") else raw[2:] if international else raw
    if not _DIGITS.fullmatch(digits):
        raise DestinationError("invalid_handle")
    national = digits.lstrip("0")
    if not international and len(national) in (10, 11) and _area(national[:2]):
        digits = "55" + national
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise DestinationError("invalid_handle")
    if digits.startswith("55"):
        rest = digits[2:]
        if len(rest) == 10 and _area(rest[:2]) and rest[2] in "6789":
            digits = "55" + rest[:2] + "9" + rest[2:]
    return "phone:+" + digits


def _area(code: str) -> bool:
    """A Brazilian area code: two digits, neither of them zero."""
    return len(code) == 2 and code[0] != "0" and code[1] != "0"


def _unb64(text: str) -> bytes:
    """Strict base64url without padding."""
    if not _BASE64URL.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("the salt is not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
