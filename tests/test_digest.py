"""The canonical JSON the digest hashes, against a plain reading of RFC 8785 on generated values."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from niadra.turns.digest import canonical


def _es_number(value: float) -> str:
    """ECMAScript's Number::toString, straight from its definition."""
    if value == 0:
        return "0"
    sign, digits, exponent = Decimal(repr(value)).as_tuple()
    assert isinstance(exponent, int)
    text = "".join(map(str, digits)).rstrip("0")
    exponent += len(digits) - len(text)
    k, n = len(text), exponent + len(text)
    minus = "-" if sign else ""
    if k <= n <= 21:
        return minus + text + "0" * (n - k)
    if 0 < n <= 21:
        return minus + text[:n] + "." + text[n:]
    if -6 < n <= 0:
        return minus + "0." + "0" * -n + text
    mantissa = text if k == 1 else text[0] + "." + text[1:]
    return f"{minus}{mantissa}e{'+' if n - 1 > 0 else '-'}{abs(n - 1)}"


def _reference(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value) if abs(value) <= 2**53 else _es_number(float(value))
    if isinstance(value, float):
        return _es_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_reference(v) for v in value) + "]"
    keys = sorted(value, key=lambda k: k.encode("utf-16-be"))
    return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _reference(value[k]) for k in keys) + "}"


_text = st.text(st.characters(exclude_categories=("Cs",)), max_size=12)
_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**60), max_value=2**60)
    | st.floats(allow_nan=False, allow_infinity=False)
    | _text
)
_values = st.recursive(
    _scalars,
    lambda inner: st.lists(inner, max_size=5) | st.dictionaries(_text, inner, max_size=5),
    max_leaves=40,
)


@settings(max_examples=500, deadline=None)
@given(_values)
def test_the_canonical_form_is_rfc_8785(value: Any) -> None:
    assert canonical(value).decode() == _reference(value)
