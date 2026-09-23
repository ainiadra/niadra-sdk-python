from __future__ import annotations

import pytest

from niadra import (
    HandleType,
    SubjectKind,
    anonymous,
    app_user,
    email,
    phone,
    system_id,
    whatsapp,
    whatsapp_bsuid,
)
from niadra.models import ObjectRef, TargetModel


def test_phone_strips_formatting_into_e164() -> None:
    handle = phone("+55 (11) 91234-5678")
    assert handle.type is HandleType.PHONE_E164
    assert handle.value == "+5511912345678"


def test_phone_accepts_the_00_international_prefix() -> None:
    assert phone("0044 20 7946 0958").value == "+442079460958"


@pytest.mark.parametrize("raw", ["11 91234-5678", "+0 123", "+55abc", "", "+1234567890123456"])
def test_phone_refuses_numbers_it_would_have_to_guess(raw: str) -> None:
    with pytest.raises(ValueError, match="international format"):
        phone(raw)


def test_email_is_trimmed_and_lowercased() -> None:
    assert email("  Marina@Example.COM ").value == "marina@example.com"
    with pytest.raises(ValueError, match="e-mail"):
        email("marina")


def test_whatsapp_keeps_digits_only() -> None:
    handle = whatsapp("+55 11 91234-5678")
    assert (handle.type, handle.value) == (HandleType.WA_ID, "5511912345678")
    with pytest.raises(ValueError, match="WhatsApp"):
        whatsapp("marina")


def test_scoped_handles_carry_their_namespace() -> None:
    bsuid = whatsapp_bsuid("BR.123", "waba-1")
    assert (bsuid.type, bsuid.scope) == (HandleType.WA_BSUID, "waba-1")
    crm = system_id("crm", "C-1042", kind="account")
    assert (crm.type, crm.scope, crm.value, crm.subject_kind) == (
        HandleType.SYSTEM_ID,
        "crm",
        "C-1042",
        SubjectKind.ACCOUNT,
    )


def test_app_and_anonymous_handles() -> None:
    assert app_user("u-1").type is HandleType.APP_USER_ID
    assert anonymous("visitor-9").type is HandleType.ANON_ID


def test_object_shorthand_keeps_colons_in_the_id() -> None:
    ref = ObjectRef.parse("ticket:zendesk:2026:81")
    assert (ref.type, ref.namespace, ref.id) == ("ticket", "zendesk", "2026:81")
    with pytest.raises(ValueError, match="type:namespace:id"):
        ObjectRef.parse("invoice:0823")


def test_target_shorthand_splits_on_the_first_slash() -> None:
    target = TargetModel.parse("openrouter/anthropic/claude")
    assert (target.provider, target.model) == ("openrouter", "anthropic/claude")
