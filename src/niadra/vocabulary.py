"""The contract vocabulary.

Every value here travels through the public API unchanged, so these enums mirror the
server's exactly. They subclass `str`, which means a plain string such as `"V1"` is
accepted anywhere an enum member is.
"""

from __future__ import annotations

import warnings
from enum import Enum
from typing import TYPE_CHECKING, Any


class _StrEnum(str, Enum):
    def __str__(self) -> str:
        return str(self.value)


class EventKind(_StrEnum):
    MESSAGE = "message"
    SYSTEM_EVENT = "system_event"
    ACTION = "action"


class Speaker(_StrEnum):
    CUSTOMER = "customer"
    AI_AGENT = "ai_agent"
    HUMAN_AGENT = "human_agent"
    SYSTEM = "system"


class Visibility(_StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"


class SubjectKind(_StrEnum):
    PERSON = "person"
    ACCOUNT = "account"
    PARTNER = "partner"


class HandleType(_StrEnum):
    PHONE_E164 = "phone_e164"
    WA_ID = "wa_id"
    WA_JID = "wa_jid"
    WA_LID = "wa_lid"
    WA_BSUID = "wa_bsuid"
    EMAIL = "email"
    GOV_ID_HMAC = "gov_id_hmac"
    APP_USER_ID = "app_user_id"
    SYSTEM_ID = "system_id"
    ORG_REGISTRY_HMAC = "org_registry_hmac"
    EMAIL_DOMAIN = "email_domain"
    ANON_ID = "anon_id"


class AssertionMethod(_StrEnum):
    EXPLICIT_IDENTIFY = "explicit_identify"
    OTP = "otp"
    LOGIN = "login"
    SYSTEM_IMPORT = "system_import"
    SAME_EVENT = "same_event"
    CO_OCCURRENCE = "co_occurrence"
    CHANNEL_ROTATION = "channel_rotation"
    ACCEPTED_SUGGESTION = "accepted_suggestion"
    EXTERNAL_RESOLVER = "external_resolver"
    DECLARED = "declared"


class Verification(_StrEnum):
    """Session verification levels. `no_customer` sits outside the V0 to V4 scale."""

    V0 = "V0"
    V1 = "V1"
    V2 = "V2"
    V3 = "V3"
    V4 = "V4"
    NO_CUSTOMER = "no_customer"

    @property
    def rank(self) -> int:
        return -1 if self is Verification.NO_CUSTOMER else int(self.value[1])


class DeliveryPath(_StrEnum):
    """Which read tier answered a `context()` call.

    `holdout` means the subject is in a control group: the pack is empty on purpose and
    the SDK treats it as a normal, successful answer.
    """

    T0 = "t0"
    T1 = "t1"
    T2 = "t2"
    T3 = "t3"
    T4 = "t4"
    HOLDOUT = "holdout"
    NOT_MODIFIED = "not_modified"


if TYPE_CHECKING:
    VERIFY_METHODS: tuple[str, ...]
    """Deprecated since 0.7.0: use `typing.get_args(niadra.models.events.VerifyMethod)`."""
else:

    def __getattr__(name: str) -> Any:
        if name == "VERIFY_METHODS":
            warnings.warn(
                "niadra.vocabulary.VERIFY_METHODS is deprecated and will be removed in 0.8.0: use "
                "typing.get_args(niadra.models.events.VerifyMethod)",
                DeprecationWarning,
                stacklevel=2,
            )
            return ("otp_whatsapp", "otp_sms", "login", "kba", "network_attestation", "human_agent")
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
