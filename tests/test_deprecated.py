"""Public names kept for one more minor release: each still answers, with a `DeprecationWarning` that names
what replaces it."""

import typing

import pytest

import niadra.models.context
import niadra.vocabulary
from niadra.models.context import HistoryItemKind
from niadra.models.events import VerifyMethod


def test_verify_methods_warns_and_keeps_its_value() -> None:
    with pytest.warns(DeprecationWarning, match="VerifyMethod"):
        methods = niadra.vocabulary.VERIFY_METHODS
    assert methods == typing.get_args(VerifyMethod)


def test_history_item_kinds_warns_and_keeps_its_value() -> None:
    with pytest.warns(DeprecationWarning, match="HistoryItemKind"):
        kinds = niadra.models.context.HISTORY_ITEM_KINDS
    assert kinds == typing.get_args(HistoryItemKind)


def test_an_unknown_name_is_still_an_attribute_error() -> None:
    with pytest.raises(AttributeError):
        niadra.vocabulary.NOT_A_NAME  # type: ignore[attr-defined]  # noqa: B018
    with pytest.raises(AttributeError):
        niadra.models.context.NOT_A_NAME  # type: ignore[attr-defined]  # noqa: B018
