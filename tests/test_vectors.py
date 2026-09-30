"""The conformance vectors of the open specifications, which the server and both SDKs run alike.

`scripts/sync_spec.py --spec` copies them into spec/vectors (and the claim contract examples, with their
negative corpus, into spec/examples/claim-contract). `EXPECTED` lists every file the SDK runs, with the fields
its spec gives a case, and its runner. Nothing here passes without running: a missing file, a file nothing
expects, a case field its spec does not define and a malformed envelope fail.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from niadra.claims import (
    Anchor,
    Mention,
    Output,
    Role,
    Turn,
    TurnValue,
    check,
    detected,
    mentions,
    nature_of,
    normalize,
    roles_of,
    score,
)
from niadra.claims.anchor import distance
from niadra.constraints.render import Binding, BindingArg, Call, honored, render
from niadra.coordination.destination import DestinationError, canonical_destination, suppression_key
from niadra.coordination.token import ContactTokenError, verify_contact_token
from niadra.exposure import ExposureTokenError, exposure_token, parse_exposure_token
from niadra.introspect import DeriveError, changes, derive
from niadra.models.signals import ConstraintsBlock
from niadra.models.state import ClaimContractSummary, StateView
from niadra.replay.counterfactual import overlap_at_k
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
from niadra.turns.capture import TurnFrame
from niadra.turns.claims import block_values, evidence
from niadra.turns.digest import canonical, digest
from niadra_mock.replay import scenario_verdict

SPEC = Path(__file__).resolve().parents[1] / "spec"
VECTORS = SPEC / "vectors"
CASE_ID = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_-]+)+$")

Runner = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class Expected:
    """A vector file the SDK runs: the fields its spec gives a case and its `expect`, and the runner."""

    case_fields: frozenset[str]
    expect_fields: frozenset[str]
    run: Runner


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


def _turn_value(item: dict[str, Any]) -> TurnValue:
    known = {"class", "value", "role", "fresh", "object_type", "name", "call_id", "ref", "declared_gaps"}
    assert set(item) <= known, sorted(set(item) - known)
    return TurnValue(
        item["class"],
        item["value"],
        item.get("role"),
        item.get("fresh", True),
        item.get("object_type"),
        item.get("name"),
        item.get("call_id"),
        item.get("ref"),
        tuple(item.get("declared_gaps", ())),
    )


def _claim_parser(case: dict[str, Any]) -> None:
    text, lang = case["text"], case["lang"]
    found = mentions(text, lang)
    roles: dict[Mention, Role] = {}
    if "roles" in case:
        for cls in {m.cls for m in found if m.cls != "label"}:
            same = [m for m in found if m.cls == cls]
            roles.update(zip(same, roles_of(text, same, case["roles"]), strict=True))
    values = tuple(_turn_value(item) for item in case.get("evidence", ()))
    got = []
    for m in found:
        item: dict[str, Any] = {"span": [m.start, m.end], "text": text[m.start : m.end], "class": m.cls}
        if m.cls != "label":
            item |= {"value": m.value(), "written": m.written}
            if "roles" in case:
                item |= {"role": roles[m].name, "role_status": roles[m].status}
            if "evidence" in case:
                item["nature"] = nature_of(text, m, roles.get(m, Role(None, "none")), values)
        got.append(item)
    assert got == case["expect"]["mentions"]


def _claim_detect(case: dict[str, Any]) -> None:
    contract = ClaimContractSummary.model_validate(json.loads((SPEC / case["contract"]).read_text()))
    out, turn = case["output"], case["turn"]
    assert set(out) <= {"text", "lang", "context", "immutable", "agent"}, sorted(out)
    assert set(turn) <= {"values", "tools", "documents", "anchors", "sections"}, sorted(turn)
    values = tuple(_turn_value(item) for item in turn.get("values", ()))
    assert all(set(a) <= {"span", "quote", "document"} for a in turn.get("anchors", ()))
    anchors = tuple(
        Anchor(a["span"][0], a["span"][1], a["quote"], a["document"]) for a in turn.get("anchors", ())
    )
    sections = {name: tuple((s, e) for s, e in spans) for name, spans in turn.get("sections", {}).items()}
    output = Output(out["text"], out["lang"], out["context"], out["immutable"], out.get("agent"))
    found = check(
        contract.categories,
        output,
        Turn(values, tuple(turn.get("tools", ())), turn.get("documents", {}), anchors, sections),
    )
    got = []
    for f in found:
        item: dict[str, Any] = {
            "category": f.category,
            "span": [f.start, f.end],
            "text": out["text"][f.start : f.end],
        }
        if f.cls is not None:
            item |= {
                "class": f.cls,
                "nature": f.nature,
                "role": f.role,
                "value": dict(f.value) if f.value else None,
            }
        item |= {"verdict": f.verdict, "action": f.action}
        if isinstance(f.evidence, TurnValue):
            item["evidence"] = {"value": values.index(f.evidence)}
        elif isinstance(f.evidence, Anchor):
            item["evidence"] = {"anchor": anchors.index(f.evidence)}
        got.append(item)
    assert got == case["expect"]["findings"]


def _claim_evidence(case: dict[str, Any]) -> None:
    contract = ClaimContractSummary.model_validate(json.loads((SPEC / case["contract"]).read_text()))
    out, blocks = case["output"], case["blocks"]
    assert set(out) <= {"text", "lang", "context", "immutable", "agent"}, sorted(out)
    assert set(blocks) <= {"state", "constraints"}, sorted(blocks)
    state = StateView.model_validate(blocks["state"]) if "state" in blocks else None
    constraints = ConstraintsBlock.model_validate(blocks["constraints"]) if "constraints" in blocks else None
    frame = TurnFrame(None, agent=out.get("agent"))
    frame.observe_state(block_values(state, constraints))
    spoken = [m for m in mentions(out["text"], out["lang"]) if m.cls != "label"]
    output = Output(out["text"], out["lang"], out["context"], out["immutable"], out.get("agent"))
    got = []
    for f in check(contract.categories, output, evidence(frame, contract, spoken, out["lang"])):
        item: dict[str, Any] = {
            "category": f.category,
            "span": [f.start, f.end],
            "text": out["text"][f.start : f.end],
        }
        if f.cls is not None:
            item |= {
                "class": f.cls,
                "nature": f.nature,
                "role": f.role,
                "value": dict(f.value) if f.value else None,
            }
        item |= {"verdict": f.verdict, "action": f.action}
        if isinstance(f.evidence, TurnValue):
            item["evidence"] = {k: v for k, v in (("ref", f.evidence.ref), ("field", f.evidence.name)) if v}
        got.append(item)
    assert got == case["expect"]["findings"]


def _claim_anchor(case: dict[str, Any]) -> None:
    expect = case["expect"]
    quote = normalize(case["quote"])
    assert len(quote) == expect["normalized_quote_length"]
    assert distance(quote, normalize(case["document"])) == expect["distance"]
    got = score(case["quote"], case["document"])
    assert got == (1 - expect["distance"] / len(quote) if quote else 0.0)
    assert (got >= 0.90) == expect["holds"]


def _suppression_key(case: dict[str, Any]) -> None:
    try:
        text = canonical_destination(case["type"], case["value"])
        got = {"canonical": text, "key": suppression_key(case["salt"], text)}
    except DestinationError as refused:
        got = {"error": refused.code}
    assert got == case["expect"]


def _contact_token(case: dict[str, Any]) -> None:
    if case["op"] == "issue":
        # Niadra issues; the SDK only checks. The runner signs as the spec says, to prove the byte form the
        # check reads is the one every issuer writes.
        assert set(case) == {"id", "op", "description", "seed", "claims", "expect"}
        seed = base64.urlsafe_b64decode(case["seed"] + "=")
        payload = _b64url(json.dumps(case["claims"], separators=(",", ":")).encode())
        signed = f"nct1.{payload}".encode()
        signature = _b64url(Ed25519PrivateKey.from_private_bytes(seed).sign(signed))
        assert {"token": f"nct1.{payload}.{signature}"} == case["expect"]
        return
    assert case["op"] == "verify"
    gateway = case["gateway"]
    try:
        claims = verify_contact_token(
            case["token"],
            keys=case["keys"],
            gateway_id=gateway["gateway_id"],
            space=gateway["space"],
            gateway_key=gateway["key"],
            destination=case["destination"],
            channel=case["channel"],
            now=case["now"],
            seen=frozenset(case["seen_jti"]),
        )
        got: dict[str, Any] = {"claims": asdict(claims)}
    except ContactTokenError as refused:
        got = {"error": refused.code}
    assert got == case["expect"]


def _counterfactual_overlap(case: dict[str, Any]) -> None:
    assert overlap_at_k(case["a"], case["b"], case["k"]) == pytest.approx(case["expect"]["overlap"], abs=1e-6)


def _type_derive(case: dict[str, Any]) -> None:
    if case["op"] == "changes":
        assert changes(case["declared"], case["live"]) == case["expect"]["changes"]
        return
    options = case["options"]
    assert set(options) <= {"type", "system", "ownership"}

    def run() -> Any:
        return derive(
            case["catalog"],
            type_name=options.get("type"),
            system=options.get("system"),
            ownership=options.get("ownership", "subject"),
        )

    if "error" in case["expect"]:
        with pytest.raises(DeriveError) as refused:
            run()
        assert refused.value.code == case["expect"]["error"]
        return
    got = run()
    assert {"fingerprint": got.fingerprint, "type": got.type, "review": got.review} == case["expect"]


def _regression_stats(case: dict[str, Any]) -> None:
    # The statistic is the recorder's; the emulator computes it the same way, and its routes serve it.
    for execution in case["executions"]:
        assert set(execution) == {"status", "paraphrase", "outcomes"}
    assert scenario_verdict(case["executions"], case["baseline"]) == case["expect"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _exposure_token(case: dict[str, Any]) -> None:
    if case["op"] == "build":
        assert set(case) == {"id", "op", "description", "exposure_id", "position", "expect"}
        token = exposure_token(case["exposure_id"], case["position"])
        assert {"token": token} == case["expect"]
        assert len(token) == 32 + len(str(case["position"]))
        return
    assert case["op"] == "parse"
    assert set(case) == {"id", "op", "description", "token", "expect"}
    try:
        exposure_id, position = parse_exposure_token(case["token"])
        got: dict[str, Any] = {"exposure_id": exposure_id, "position": position}
    except ExposureTokenError as refused:
        got = {"error": refused.code}
    assert got == case["expect"]


def _binding(raw: dict[str, Any], families: dict[str, str]) -> Binding:
    assert set(raw) <= {"tool", "args", "overfetch"}
    args = []
    for arg in raw["args"]:
        assert set(arg) <= {"attr", "param", "transform", "negation", "ops"}
        negation = arg.get("negation")
        args.append(
            BindingArg(
                arg["attr"],
                arg["param"],
                arg.get("transform"),
                negation["param"] if negation else None,
                tuple(arg.get("ops", ())),
                families.get(arg["attr"]),
            )
        )
    return Binding(raw["tool"], tuple(args), raw.get("overfetch", False))


def _constraint_render(case: dict[str, Any]) -> None:
    block = ConstraintsBlock.model_validate(case["block"])
    raw_call = case["call"]
    assert set(raw_call) <= {"args", "for", "category", "asked"}
    call = Call(
        raw_call["args"], raw_call["for"], raw_call.get("category"), frozenset(raw_call.get("asked", ()))
    )
    got = render(block, _binding(case["binding"], case["families"]), call, case["mode"])
    rendered: dict[str, Any] = {
        "applies": got.applies,
        "args": dict(got.args),
        "suggested": dict(got.suggested),
        "injected": list(got.injected),
        "hard_sent": list(got.hard_sent),
        "residual": list(got.residual),
        "post_filter": list(got.post_filter),
        "conflicts": [{"id": i, "param": p} for i, p in got.conflicts],
    }
    if "results" in case:
        seen = honored(block, got.hard_sent, case["results"])
        rendered["honored"] = {
            "results_checked": seen.results_checked,
            "violations": seen.violations,
            "unverifiable": seen.unverifiable,
        }
    assert rendered == case["expect"]
    inferred = {a.id for a in block.attributes if a.source not in ("stated", "correction")}
    assert not inferred & set(got.injected), "an inferred attribute is never injected, whatever the mode"


EXPECTED: dict[str, Expected] = {
    "turn-record-digest.v0": Expected(
        frozenset({"id", "note", "value", "expect"}), frozenset({"canonical", "sha256", "size"}), _digest
    ),
    "niadra-expr.v0": Expected(_EXPR_CASE, frozenset({"value"}), _expr),
    "claim-parser.v0": Expected(
        frozenset({"id", "lang", "text", "roles", "evidence", "expect"}),
        frozenset({"mentions"}),
        _claim_parser,
    ),
    "claim-detect.v0": Expected(
        frozenset({"id", "contract", "output", "turn", "expect"}), frozenset({"findings"}), _claim_detect
    ),
    "claim-evidence.v0": Expected(
        frozenset({"id", "contract", "output", "blocks", "expect"}), frozenset({"findings"}), _claim_evidence
    ),
    "claim-anchor.v0": Expected(
        frozenset({"id", "quote", "document", "expect"}),
        frozenset({"normalized_quote_length", "distance", "holds"}),
        _claim_anchor,
    ),
    "constraint-render.v0": Expected(
        frozenset({"id", "block", "binding", "families", "call", "mode", "results", "expect"}),
        frozenset(
            {"applies", "args", "suggested", "injected", "hard_sent", "residual", "post_filter", "conflicts"}
            | {"honored"}
        ),
        _constraint_render,
    ),
    "exposure-token.v0": Expected(
        frozenset({"id", "op", "description", "exposure_id", "position", "token", "expect"}),
        frozenset({"token", "exposure_id", "position"}),
        _exposure_token,
    ),
    "contact-token.v0": Expected(
        frozenset({"id", "op", "description", "seed", "claims", "keys", "gateway", "token"})
        | frozenset({"destination", "channel", "now", "seen_jti", "expect"}),
        frozenset({"token", "claims"}),
        _contact_token,
    ),
    "counterfactual-overlap.v0": Expected(
        frozenset({"id", "description", "a", "b", "k", "expect"}),
        frozenset({"overlap"}),
        _counterfactual_overlap,
    ),
    "type-derive.v0": Expected(
        frozenset({"id", "op", "description", "catalog", "options", "declared", "live", "expect"}),
        frozenset({"fingerprint", "type", "review", "changes"}),
        _type_derive,
    ),
    "regression-stats.v0": Expected(
        frozenset({"id", "description", "executions", "baseline", "expect"}),
        frozenset({"verdict", "completed", "infrastructure_errors", "pin_mismatches", "needs_paraphrase"})
        | frozenset({"assertions"}),
        _regression_stats,
    ),
    "suppression-key.v0": Expected(
        frozenset({"id", "description", "salt", "type", "value", "expect"}),
        frozenset({"canonical", "key"}),
        _suppression_key,
    ),
}
NEGATIVE_CORPUS = ("retail", "legal", "health-plan-sales")


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((VECTORS / f"{name}.json").read_text())
    return data


def _cases() -> list[Any]:
    return [
        pytest.param(name, case, id=f"{name}:{case.get('id')}")
        for name in EXPECTED
        for case in _load(name)["cases"]
    ]


def test_every_published_vector_file_is_expected() -> None:
    published = {path.stem for path in VECTORS.glob("*.json")}
    unknown = published - set(EXPECTED)
    assert not unknown, f"vector files the SDK does not run: {sorted(unknown)}; add them to EXPECTED"


@pytest.mark.parametrize("name", EXPECTED)
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
    EXPECTED[name].run(case)


@pytest.mark.parametrize("sector", NEGATIVE_CORPUS)
def test_the_negative_corpus_never_triggers_the_claim_contract(sector: str) -> None:
    document = json.loads((SPEC / "examples" / "claim-contract" / f"{sector}.json").read_text())
    phrases = document["negative_corpus"]["phrases"]
    assert phrases, f"{sector}: an empty negative corpus proves nothing"
    contract = ClaimContractSummary.model_validate(document)
    # A phrase triggers when, in any of the contract's languages and for any of its agents, a category
    # finds a claim in it (the claim contract spec, section 10.2).
    agents = {None, *(a for c in contract.categories for a in c.agents)}
    triggered = [
        (phrase, lang, agent, categories)
        for phrase in phrases
        for lang in contract.languages
        for agent in sorted(agents, key=str)
        if (categories := detected(contract.categories, Output(phrase, lang, "chat", False, agent)))
    ]
    assert not triggered


def test_the_negative_corpus_check_would_catch_a_phrase_that_triggers() -> None:
    document = json.loads((SPEC / "examples" / "claim-contract" / "retail.json").read_text())
    contract = ClaimContractSummary.model_validate(document)
    assert detected(contract.categories, Output("Infelizmente está esgotado.", "pt", "chat", False)) == [
        "availability_denial"
    ]
