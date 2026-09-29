"""`niadra contract test`: the space's claim contract against the company's negative corpus and example turns,
for the company's CI (the claim contract spec, section 10).

    niadra contract test --contract claim-contract.json --examples tests/claims/*.json

The contract is the company's own copy (`--contract`, the document it submits as configuration, with its
negative corpus), or the one the space serves in its SDK profile, with the corpus from `--corpus`: the
profile never carries the corpus's phrases. A phrase of the corpus fails the test when, in any of the
contract's languages and for any of its agents, a category finds a claim in it: a false positive that blocks
a document with a deadline running is the main risk of the contract.

An examples file holds `{"examples": [...]}`, each example `{"id", "output", "turn", "expect"}`:
- `output` is `{"text", "lang", "context", "immutable", "agent"}`;
- `turn` is what the turn held (`values`, `tools`, `documents`, `anchors`, `sections`), as in the claim
  detection vectors;
- `expect` is `{"claims": [...]}`: each expected claim names its `category` and, when given, its `verdict`
  and `action`, in the order the output says them. An empty list expects no claim.

Exits with 0 when everything holds, 1 when a phrase triggers or an example differs, and 2 when it could not
run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from niadra._transport import Request
from niadra.claims import Anchor, Output, Turn, TurnValue, check, detected
from niadra.models.state import ClaimContractSummary

if TYPE_CHECKING:
    from niadra._client import Niadra


@dataclass
class ContractReport:
    """What the test found: the phrases that triggered (with the language, the agent and the categories) and
    the examples that differ (with what was found instead)."""

    contract: str
    corpus: str | None
    phrases: int
    examples: int
    triggered: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.triggered and not self.failed


def add(commands: Any) -> None:
    contract = commands.add_parser("contract", help="the claim contract, for the company's CI")
    verbs = contract.add_subparsers(dest="verb", required=True)
    test = verbs.add_parser("test", help="the negative corpus and example turns against the contract")
    test.add_argument("--contract", type=Path, help="the company's copy of the contract; else the profile's")
    test.add_argument("--corpus", type=Path, help="the negative corpus, when --contract does not carry it")
    test.add_argument("--examples", type=Path, nargs="*", default=[], help="example turn files")


def run(args: argparse.Namespace, client: Callable[[], Niadra]) -> int:
    try:
        if args.contract is not None:
            document = _read(args.contract)
            contract = ClaimContractSummary.model_validate(document)
        else:
            profile = client()._transport.request(Request("GET", "/v1/sdk/profile")) or {}
            served = profile.get("claim_contract")
            if served is None:
                return _fail("the space serves no claim contract: pass --contract")
            document, contract = {}, ClaimContractSummary.model_validate(served)
        # A corpus file is a contract document, or the corpus alone: {version, phrases}.
        source = _read(args.corpus) if args.corpus is not None else document
        corpus = source.get("negative_corpus", source) if isinstance(source, Mapping) else None
        if not isinstance(corpus, Mapping) or not corpus.get("phrases"):
            return _fail("no negative corpus to test: --contract with one, or --corpus")
        examples = [example for path in args.examples for example in _examples(_read(path))]
    except (OSError, ValueError) as error:
        return _fail(f"could not read the contract, the corpus or the examples ({type(error).__name__})")
    except Exception as error:
        return _fail(f"could not read the contract ({type(error).__name__})")
    version = corpus.get("version")
    if contract.negative_corpus_version and version and version != contract.negative_corpus_version:
        print(
            f"niadra: the contract names corpus {contract.negative_corpus_version}, the file is {version}",
            file=sys.stderr,
        )
    report = check_contract(contract, corpus["phrases"], examples, corpus=version)
    for item in report.triggered:
        print(f"triggered: {item['phrase']!r} ({item['lang']}, {item['categories']})", file=sys.stderr)
    for item in report.failed:
        print(f"example {item['id']}: expected {item['expected']}, found {item['found']}", file=sys.stderr)
    print(
        json.dumps(
            {
                "contract": report.contract,
                "corpus": report.corpus,
                "phrases": report.phrases,
                "triggered": len(report.triggered),
                "examples": report.examples,
                "failed": len(report.failed),
            },
            indent=2,
        )
    )
    return 0 if report.passed else 1


def check_contract(
    contract: ClaimContractSummary,
    phrases: Sequence[str],
    examples: Iterable[Mapping[str, Any]] = (),
    *,
    corpus: str | None = None,
) -> ContractReport:
    """Runs the corpus and the examples against `contract`."""
    agents = sorted({None, *(a for c in contract.categories for a in c.agents)}, key=str)
    report = ContractReport(contract.version, corpus, len(phrases), 0)
    for phrase in phrases:
        for lang in contract.languages:
            for agent in agents:
                found = detected(contract.categories, Output(phrase, lang, "chat", False, agent))
                if found:
                    report.triggered.append(
                        {"phrase": phrase, "lang": lang, "agent": agent, "categories": sorted(found)}
                    )
    for example in examples:
        report.examples += 1
        expected = list(example["expect"]["claims"])
        claims = [
            {"category": f.category, "verdict": f.verdict, "action": f.action}
            for f in check(contract.categories, _output(example["output"]), _turn(example.get("turn") or {}))
        ]
        compared = [
            {k: v for k, v in got.items() if k in want} for got, want in zip(claims, expected, strict=False)
        ]
        if len(claims) != len(expected) or compared != expected:
            report.failed.append({"id": example["id"], "expected": expected, "found": claims})
    return report


def _examples(document: Any) -> list[Mapping[str, Any]]:
    items: Any = document.get("examples") if isinstance(document, Mapping) else document
    shaped = isinstance(items, list) and all(
        isinstance(i, Mapping) and {"id", "output", "expect"} <= set(i) and "claims" in i["expect"]
        for i in items
    )
    if not shaped:
        raise ValueError("an examples file holds {'examples': [{id, output, turn, expect}]}")
    return list(items)


def _output(item: Mapping[str, Any]) -> Output:
    context = item.get("context", "chat")
    return Output(item["text"], item["lang"], context, bool(item.get("immutable")), item.get("agent"))


def _turn(item: Mapping[str, Any]) -> Turn:
    values = tuple(
        TurnValue(
            v["class"],
            v["value"],
            v.get("role"),
            v.get("fresh", True),
            v.get("object_type"),
            v.get("name"),
            v.get("call_id"),
            v.get("ref"),
            tuple(v.get("declared_gaps", ())),
        )
        for v in item.get("values", ())
    )
    anchors = tuple(
        Anchor(a["span"][0], a["span"][1], a["quote"], a["document"]) for a in item.get("anchors", ())
    )
    sections = {name: tuple((s, e) for s, e in spans) for name, spans in item.get("sections", {}).items()}
    return Turn(values, tuple(item.get("tools", ())), dict(item.get("documents", {})), anchors, sections)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _fail(message: str) -> int:
    print(f"niadra: {message}", file=sys.stderr)
    return 2
