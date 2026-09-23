"""Helpers that build `Handle`s with the normalization the server expects.

They raise `ValueError` on input that cannot be a valid identifier, so a malformed phone
number fails where it enters your code rather than as a rejected event later.
"""

from __future__ import annotations

import re

from niadra.models.common import Handle
from niadra.vocabulary import HandleType, SubjectKind

_PHONE_NOISE = re.compile(r"[\s().\-]")
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def phone(number: str) -> Handle:
    """A phone number in E.164. Spaces, dots, dashes and parentheses are dropped.

    The leading `+` and country code are required: `phone("+55 11 91234-5678")`. Local
    formats are ambiguous across countries, so the SDK does not guess.
    """
    value = _PHONE_NOISE.sub("", number)
    if value.startswith("00"):
        value = "+" + value[2:]
    if not _E164.match(value):
        raise ValueError("phone numbers must be in international format, e.g. +5511912345678")
    return Handle(type=HandleType.PHONE_E164, value=value)


def email(address: str) -> Handle:
    """An e-mail address, trimmed and lowercased."""
    value = address.strip().lower()
    if not _EMAIL.match(value):
        raise ValueError("not an e-mail address")
    return Handle(type=HandleType.EMAIL, value=value)


def whatsapp(wa_id: str) -> Handle:
    """A WhatsApp id: the phone number in digits, as the Cloud API reports it in `wa_id`.

    A leading `+` and separators are accepted and removed.
    """
    value = _PHONE_NOISE.sub("", wa_id).removeprefix("+")
    if not value.isdigit() or not 7 <= len(value) <= 15:
        raise ValueError("a WhatsApp id is the phone number in digits, e.g. 5511912345678")
    return Handle(type=HandleType.WA_ID, value=value)


def whatsapp_bsuid(user_id: str, business_account: str) -> Handle:
    """A WhatsApp business-scoped user id, which is only unique within one business account."""
    return Handle(type=HandleType.WA_BSUID, value=user_id.strip(), scope=business_account)


def system_id(namespace: str, id: str, *, kind: SubjectKind | str | None = None) -> Handle:
    """The id of a subject in one of your systems, e.g. `system_id("crm", "C-1042")`.

    Pass `kind="account"` or `kind="partner"` for organizations; people are the default.
    """
    return Handle(
        type=HandleType.SYSTEM_ID,
        value=id,
        scope=namespace,
        subject_kind=SubjectKind(kind) if kind is not None else None,
    )


def app_user(user_id: str) -> Handle:
    """The id of a signed-in user of your app."""
    return Handle(type=HandleType.APP_USER_ID, value=user_id)


def anonymous(visitor_id: str) -> Handle:
    """A visitor who has not identified yet. Link it to a real handle later with `identify()`."""
    return Handle(type=HandleType.ANON_ID, value=visitor_id)
