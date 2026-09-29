"""The digest of a value a turn record carries: SHA-256 over its canonical JSON (RFC 8785).

The canonical form leaves no whitespace, sorts object keys by their UTF-16 code units and writes numbers as
ECMAScript does, so a producer in any language hashes the same value to the same digest. The Turn Record
spec fixes it (section 6.2.1) with the vectors in `spec/vectors/turn-record-digest.v0.json`.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from decimal import Decimal
from json.encoder import encode_basestring as _string
from typing import Any

# Integers beyond this lose precision as the IEEE doubles JSON numbers are; they are written as doubles.
_EXACT = 2**53


def canonical(value: Any) -> bytes:
    """`value` (JSON data: dicts with string keys, lists, strings, numbers, booleans and None) as the UTF-8
    bytes of its canonical JSON. A value JSON cannot hold (NaN, a non-string key, a datetime) raises."""
    out: list[str] = []
    _append(value, out.append)
    return "".join(out).encode("utf-8")


def digest(value: Any) -> tuple[str, int]:
    """`value`'s digest, written `sha256:<hex>`, and the size in bytes of its canonical JSON."""
    data = canonical(value)
    return "sha256:" + hashlib.sha256(data).hexdigest(), len(data)


def _append(value: Any, write: Callable[[str], None]) -> None:
    # Exact types first: the SDK hashes results of up to a few hundred kilobytes, often.
    kind = type(value)
    if kind is str:
        write(_string(value))
    elif kind is dict:
        keys = list(value)
        if not all(type(key) is str for key in keys):
            raise TypeError("canonical JSON takes only string keys")
        # Code point order is UTF-16 order unless a key holds a character past the surrogates.
        if any(key and max(key) >= "\ud800" for key in keys):
            keys.sort(key=lambda k: k.encode("utf-16-be"))
        else:
            keys.sort()
        write("{")
        for i, key in enumerate(keys):
            write("," + _string(key) + ":" if i else _string(key) + ":")
            _append(value[key], write)
        write("}")
    elif kind is list or kind is tuple:
        write("[")
        for i, item in enumerate(value):
            if i:
                write(",")
            _append(item, write)
        write("]")
    elif value is None or kind is bool:
        write("null" if value is None else "true" if value else "false")
    elif kind is int:
        write(str(value) if -_EXACT <= value <= _EXACT else _number(float(value)))
    elif kind is float:
        write(_number(value))
    elif isinstance(value, (str, int, float, dict, list, tuple)):
        # A subclass (an enum of str, an IntEnum): hashed as the plain value it is.
        _append(_plain(value), write)
    else:
        raise TypeError(f"canonical JSON has no form for {type(value).__name__}")


def _plain(value: Any) -> Any:
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, dict):
        return dict(value)
    return list(value)


def _number(value: float) -> str:
    """ECMAScript's Number::toString: the shortest digits that read back the same double, in fixed
    notation from 1e-7 up to 1e21 and in exponent notation outside it."""
    if not math.isfinite(value):
        raise ValueError("JSON has no NaN or Infinity")
    if value == 0:
        return "0"
    if value.is_integer():
        if abs(value) <= _EXACT:
            return str(int(value))
    else:
        text = repr(value)
        if "e" not in text:
            return text  # repr's shortest digits in fixed notation are ECMAScript's
    sign, digits, exponent = Decimal(repr(value)).as_tuple()
    assert isinstance(exponent, int)
    text = "".join(map(str, digits)).rstrip("0")
    exponent += len(digits) - len(text)
    k = len(text)
    n = exponent + k  # the value is 0.<text> x 10^n
    minus = "-" if sign else ""
    if k <= n <= 21:
        return minus + text + "0" * (n - k)
    if 0 < n <= 21:
        return minus + text[:n] + "." + text[n:]
    if -6 < n <= 0:
        return minus + "0." + "0" * -n + text
    e = n - 1
    mantissa = text if k == 1 else text[0] + "." + text[1:]
    return f"{minus}{mantissa}e{'+' if e > 0 else '-'}{abs(e)}"
