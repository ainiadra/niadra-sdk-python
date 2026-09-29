"""The digest of a value a turn record carries: SHA-256 over its canonical JSON (RFC 8785).

The canonical form leaves no whitespace, sorts object keys by their UTF-16 code units and writes numbers as
ECMAScript does, so a producer in any language hashes the same value to the same digest. The Turn Record
spec fixes it (section 6.2.1) with the vectors in `spec/vectors/turn-record-digest.v0.json`.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any

# Integers beyond this lose precision as the IEEE doubles JSON numbers are; they are written as doubles.
_EXACT = 2**53


def canonical(value: Any) -> bytes:
    """`value` (JSON data: dicts with string keys, lists, strings, numbers, booleans and None) as the UTF-8
    bytes of its canonical JSON. A value JSON cannot hold (NaN, a non-string key, a datetime) raises."""
    out: list[str] = []
    _append(value, out)
    return "".join(out).encode("utf-8")


def digest(value: Any) -> tuple[str, int]:
    """`value`'s digest, written `sha256:<hex>`, and the size in bytes of its canonical JSON."""
    data = canonical(value)
    return "sha256:" + hashlib.sha256(data).hexdigest(), len(data)


def _append(value: Any, out: list[str]) -> None:
    if value is None or isinstance(value, bool):
        out.append(json.dumps(value))
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, int):
        out.append(str(value) if abs(value) <= _EXACT else _number(float(value)))
    elif isinstance(value, float):
        out.append(_number(value))
    elif isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON takes only string keys")
        out.append("{")
        for i, key in enumerate(sorted(value, key=lambda k: k.encode("utf-16-be"))):
            out.append("," if i else "")
            out.append(json.dumps(key, ensure_ascii=False) + ":")
            _append(value[key], out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            out.append("," if i else "")
            _append(item, out)
        out.append("]")
    else:
        raise TypeError(f"canonical JSON has no form for {type(value).__name__}")


def _number(value: float) -> str:
    """ECMAScript's Number::toString: the shortest digits that read back the same double, in fixed
    notation from 1e-7 up to 1e21 and in exponent notation outside it."""
    if not math.isfinite(value):
        raise ValueError("JSON has no NaN or Infinity")
    if value == 0:
        return "0"
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
