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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

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


def _pending(case_fields: str, expect_fields: str, what: str) -> Expected:
    """A published file whose runner the SDK does not have yet."""
    return Expected(frozenset(case_fields.split()), frozenset(expect_fields.split()), None, pending=what)


EXPECTED: dict[str, Expected] = {
    "turn-record-digest.v0": Expected(
        frozenset({"id", "note", "value", "expect"}), frozenset({"canonical", "sha256", "size"}), _digest
    ),
    "niadra-expr.v0": _pending("id expr input expect", "value", "the niadra-expr evaluator"),
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
