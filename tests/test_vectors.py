"""The conformance vectors of the open specifications, which the server and both SDKs run alike.

`scripts/sync_spec.py --spec` copies them into spec/vectors (and the claim contract examples, with their
negative corpus, into spec/examples/claim-contract). `EXPECTED` lists every file the SDK runs, with the fields
its spec gives a case. Nothing here passes without running:

- a file not published yet is skipped with the reason "pending vectors";
- a published file whose runner the SDK does not have yet is an expected failure ("pending
  implementation"), strict, so it turns red the day a runner makes it pass and nobody moved it;
- a file nothing expects, a case field its spec does not define and a malformed envelope fail.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from niadra.state.expr import (
    Calendar,
    Environment,
    ExprError,
    Kind,
    Slot,
    Value,
    absent,
    boolean,
    epoch_ms,
    evaluate,
    format_datetime,
    parse,
    unknown,
)
from niadra.state.logic import Logic
from niadra.turns.digest import canonical, digest

SPEC = Path(__file__).resolve().parents[1] / "spec"
VECTORS = SPEC / "vectors"
CASE_ID = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_-]+)+$")

Runner = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class Expected:
    """A vector file the SDK runs: the fields its spec gives a case and its `expect`, and the runner."""

    case_fields: frozenset[str]
    expect_fields: frozenset[str]
    run: Runner | None
    pending: str | None = None


def _digest(case: dict[str, Any]) -> None:
    expect = case["expect"]
    assert canonical(case["value"]).decode() == expect["canonical"]
    assert digest(case["value"]) == (expect["sha256"], expect["size"])


# niadra-expr (spec/object-type.md, section 6): each case evaluates one expression over one environment.

_EXPR_CASE = frozenset({"id", "expr", "input", "expect"})
_EXPR_INPUT = frozenset(
    {
        "now",
        "utc_offset",
        "fields",
        "inputs",
        "absent_names",
        "config",
        "quotes",
        "calendars",
        "state",
        "derived_status",
        "watch_count",
        "purpose",
        "presented_rank",
    }
)
_EXPR_SLOT = frozenset({"type", "v", "logic", "absent", "at", "observer", "was", "completeness"})
_EXPR_PREVIOUS = frozenset({"type", "v", "logic", "absent"})
_KIND_OF_TYPE = {
    "string": Kind.STRING,
    "text": Kind.STRING,
    "enum": Kind.STRING,
    "ref": Kind.STRING,
    "number": Kind.NUMBER,
    "money": Kind.NUMBER,
    "percent": Kind.NUMBER,
    "date": Kind.DATE,
    "datetime": Kind.DATETIME,
    "duration": Kind.DURATION,
    "bool": Kind.BOOL,
    "list": Kind.LIST,
}
# Python 3.10's `datetime.fromisoformat` reads neither `Z` nor a fraction of other than 3 or 6 digits.
_RFC3339 = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})")


def _instant(text: str) -> int:
    found = _RFC3339.fullmatch(text)
    assert found, f"not an RFC 3339 time with an offset: {text!r}"
    stamp, fraction, offset = found.groups()
    micros = (fraction or "").ljust(6, "0")[:6]
    return epoch_ms(datetime.fromisoformat(f"{stamp}.{micros}{'+00:00' if offset == 'Z' else offset}"))


def _scalar(raw: Any) -> Value:
    if isinstance(raw, bool):
        return boolean(raw)
    if isinstance(raw, (int, float)):
        return Value(Logic.YES, Kind.NUMBER, float(raw))
    assert isinstance(raw, str)
    return Value(Logic.YES, Kind.STRING, raw)


def _datum(kind: Kind, raw: Any) -> object:
    if kind is Kind.NUMBER:
        return float(raw)
    if kind is Kind.DATE:
        return date.fromisoformat(raw)
    if kind is Kind.DATETIME:
        return _instant(raw)
    if kind is Kind.DURATION:
        assert isinstance(raw, int)
        return raw
    if kind is Kind.LIST:
        return tuple(_scalar(item) for item in raw)
    return raw


def _value(raw: Mapping[str, Any], keys: frozenset[str]) -> Value:
    assert set(raw) <= keys, f"unknown slot fields {sorted(set(raw) - keys)}"
    v = raw.get("v")
    kind = _KIND_OF_TYPE[raw["type"]] if "type" in raw else None
    if kind is None and v is not None:
        kind = _scalar(v).kind
    default = (Logic.YES if v is not False else Logic.NO) if v is not None else None
    logic = Logic(raw["logic"]) if "logic" in raw else default or Logic.UNOBSERVED
    if kind is Kind.BOOL and logic.known:
        return boolean(logic is Logic.YES)
    if logic is Logic.NO:
        return absent(raw.get("absent"))
    if not logic.known:
        return unknown(logic)
    assert kind is not None
    return Value(Logic.YES, kind, _datum(kind, v))


def _slot(raw: Mapping[str, Any]) -> Slot:
    assert set(raw) <= _EXPR_SLOT, f"unknown slot fields {sorted(set(raw) - _EXPR_SLOT)}"
    return Slot(
        _value({k: v for k, v in raw.items() if k in _EXPR_PREVIOUS}, _EXPR_PREVIOUS),
        at=_instant(raw["at"]) if "at" in raw else None,
        observer=raw.get("observer"),
        was=_value(raw["was"], _EXPR_PREVIOUS) if "was" in raw else None,
        completeness=raw.get("completeness"),
    )


def _offset_minutes(text: str) -> int:
    sign = -1 if text.startswith("-") else 1
    hours, minutes = text[1:].split(":")
    return sign * (int(hours) * 60 + int(minutes))


def _environment(raw: Mapping[str, Any]) -> Environment:
    assert set(raw) <= _EXPR_INPUT, f"unknown input fields {sorted(set(raw) - _EXPR_INPUT)}"
    return Environment(
        now=_instant(raw["now"]),
        utc_offset_min=_offset_minutes(raw.get("utc_offset", "+00:00")),
        fields={name: _slot(slot) for name, slot in raw.get("fields", {}).items()},
        inputs={name: _slot(slot) for name, slot in raw.get("inputs", {}).items()},
        absent_names=frozenset(raw.get("absent_names", [])),
        config={key: absent() if v is None else _scalar(v) for key, v in raw.get("config", {}).items()},
        quotes={
            source: {name: _slot(slot) for name, slot in quote.items()}
            for source, quote in raw.get("quotes", {}).items()
        },
        calendars={
            name: Calendar(
                holidays=frozenset(date.fromisoformat(day) for day in calendar.get("holidays", [])),
                weekend=frozenset(calendar.get("weekend", [6, 7])),
            )
            for name, calendar in raw.get("calendars", {}).items()
        },
        state=raw.get("state"),
        derived_status=raw.get("derived_status"),
        watch_count=raw.get("watch_count", 0),
        purpose=raw.get("purpose", "display"),
        presented_rank=raw.get("presented_rank"),
    )


def _encode(value: Value) -> dict[str, Any]:
    """A result as the vectors write it: the logical value, and the kind and datum of a known value."""
    if not value.logic.known:
        return {"logic": value.logic.value}
    if value.is_absent:
        return {"logic": "no", **({"absent": value.absent} if value.absent else {})}
    assert value.kind is not None
    out: dict[str, Any] = {"type": value.kind.value, "logic": value.logic.value}
    if value.kind is Kind.DATE:
        assert isinstance(value.datum, date)
        out["v"] = value.datum.isoformat()
    elif value.kind is Kind.DATETIME:
        assert isinstance(value.datum, int)
        out["v"] = format_datetime(value.datum)
    elif value.kind is Kind.LIST:
        assert isinstance(value.datum, tuple)
        out["v"] = [_encode(item) for item in value.datum]
    else:
        out["v"] = value.datum
    return out


def _same(expected: Any, actual: Any) -> bool:
    """Numbers compare by value (`42` and `42.0` are one number); everything else exactly."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(_same(expected[k], actual[k]) for k in expected)
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(
            _same(e, a) for e, a in zip(expected, actual, strict=True)
        )
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if isinstance(expected, bool) or isinstance(actual, bool):
            return expected is actual
        return float(expected) == float(actual)
    return bool(expected == actual)


def _expr(case: dict[str, Any]) -> None:
    assert set(case) == _EXPR_CASE, f"unknown case fields {sorted(set(case) - _EXPR_CASE)}"
    assert set(case["expect"]) in ({"value"}, {"error"}), case["expect"]
    env = _environment(case["input"])
    try:
        result: dict[str, Any] = {"value": _encode(evaluate(parse(case["expr"]), env))}
    except ExprError as exc:
        result = {"error": exc.code}
    assert _same(case["expect"], result), result


def _pending(case_fields: str, expect_fields: str, what: str) -> Expected:
    """A published file whose runner the SDK does not have yet."""
    return Expected(frozenset(case_fields.split()), frozenset(expect_fields.split()), None, pending=what)


EXPECTED: dict[str, Expected] = {
    "turn-record-digest.v0": Expected(
        frozenset({"id", "note", "value", "expect"}), frozenset({"canonical", "sha256", "size"}), _digest
    ),
    "niadra-expr.v0": Expected(_EXPR_CASE, frozenset({"value"}), _expr),
    "claim-parser.v0": _pending(
        "id lang text roles evidence expect", "mentions", "the claim contract's number and role parser"
    ),
    "claim-detect.v0": _pending(
        "id contract output turn expect", "findings", "the claim contract's detection"
    ),
    "claim-anchor.v0": _pending(
        "id quote document expect",
        "normalized_quote_length distance holds",
        "the claim contract's text anchor",
    ),
    "constraint-render.v0": _pending(
        "id block binding families call mode results expect",
        "applies args suggested injected hard_sent residual post_filter conflicts honored",
        "the constraints block's rendering per tool binding",
    ),
    "exposure-token.v0": _pending(
        "id op description exposure_id position token expect",
        "token exposure_id position",
        "the exposure token",
    ),
    "contact-token.v0": _pending(
        "id op description seed claims keys gateway token destination channel now seen_jti expect",
        "token claims",
        "the contact token's issue and offline check",
    ),
    "suppression-key.v0": _pending(
        "id description salt type value expect", "canonical key", "the suppression list's per-source key"
    ),
}
NEGATIVE_CORPUS = ("retail", "legal", "health-plan-sales")


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((VECTORS / f"{name}.json").read_text())
    return data


def _cases() -> list[Any]:
    params = []
    for name, expected in EXPECTED.items():
        if not (VECTORS / f"{name}.json").exists():
            reason = f"pending vectors: {name}.json is not published in niadra-spec yet"
            params.append(pytest.param(name, {}, id=name, marks=pytest.mark.skip(reason=reason)))
            continue
        marks = []
        if expected.pending:
            reason = f"pending implementation: {expected.pending}"
            marks.append(pytest.mark.xfail(strict=True, raises=NotImplementedError, reason=reason))
        for case in _load(name)["cases"]:
            params.append(pytest.param(name, case, id=f"{name}:{case.get('id')}", marks=marks))
    return params


def test_every_published_vector_file_is_expected() -> None:
    published = {path.stem for path in VECTORS.glob("*.json")}
    unknown = published - set(EXPECTED)
    assert not unknown, f"vector files the SDK does not run: {sorted(unknown)}; add them to EXPECTED"


@pytest.mark.parametrize("name", [n for n in EXPECTED if (VECTORS / f"{n}.json").exists()])
def test_a_published_file_has_the_envelope_and_only_the_fields_its_spec_defines(name: str) -> None:
    data = _load(name)
    stem, version = name.rsplit(".", 1)
    assert set(data) == {"vectors", "version", "spec", "cases"}
    assert (data["vectors"], data["version"]) == (stem, version)
    assert isinstance(data["spec"], str) and data["spec"].startswith("spec/")
    ids = [case["id"] for case in data["cases"]]
    assert ids and len(ids) == len(set(ids)) and all(CASE_ID.match(i) for i in ids)
    expected = EXPECTED[name]
    for case in data["cases"]:
        assert set(case) <= expected.case_fields, f"{case['id']}: {sorted(set(case) - expected.case_fields)}"
        outcome = set(case.get("expect", {}))
        assert outcome <= expected.expect_fields | {"error"}, f"{case['id']}: {sorted(outcome)}"


@pytest.mark.parametrize(("name", "case"), _cases())
def test_case(name: str, case: dict[str, Any]) -> None:
    run = EXPECTED[name].run
    if run is None:
        raise NotImplementedError(EXPECTED[name].pending)
    run(case)


@pytest.mark.parametrize("sector", NEGATIVE_CORPUS)
def test_the_negative_corpus_never_triggers_the_claim_contract(sector: str) -> None:
    path = SPEC / "examples" / "claim-contract" / f"{sector}.json"
    if not path.exists():
        pytest.skip(f"pending vectors: examples/claim-contract/{sector}.json is not published yet")
    phrases = json.loads(path.read_text())["negative_corpus"]["phrases"]
    assert phrases, f"{sector}: an empty negative corpus proves nothing"
    pytest.xfail("pending implementation: the claim contract's detection")
