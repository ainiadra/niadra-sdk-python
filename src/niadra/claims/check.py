"""The claim check (the claim contract spec, sections 4, 7 and 8): the claims each category detects in an
output, their nature, the verdict against what the turn holds, and the action the contract takes for the
output's context.

Detection: numbers of the category's classes (in a sentence with one of its terms, when it lists terms)
that the output asserts (a hedged number, `niadra.claims.hedges`, is no claim), each occurrence of a term
(when it lists terms and no classes), statute and precedent citations, and the sentences of named document
sections. Nature, for a number: `quoted` inside quotation marks; `computed` when the turn holds a value of the
same role, or the same value; `model` otherwise. Verdicts that stand (`matched`, `quoted_found`, `anchored`)
take no action; `not_checked` is counted; every other one takes the category's action for the context, and
`unsupported` the action its natures give to what the model said. A rewrite happens only when it is
unequivocal (a stale copy of one field whose fresh value differs), and is a warning otherwise. Nothing is ever
rewritten in an immutable output, and a block there sends the whole output to a person, untouched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from niadra.claims.anchor import score
from niadra.claims.hedges import hedged
from niadra.claims.numbers import Mention, mentions
from niadra.claims.roles import Role, roles_of
from niadra.claims.text import as_words, fold, phrase_at, quotations, sentence_of, sentences, words

REWRITABLE = frozenset({"money", "percent", "date", "duration"})
"""Classes a rewrite may touch: a dose, a technical quantity, a count and a label never change."""
STANDING = frozenset({"matched", "quoted_found", "anchored"})
_RECORDED = {"rewrite_if_unequivocal": "rewrite", "discard_anchor_and_count": "discard_anchor"}
"""A contract's action, as the turn record writes what happened."""

PATTERNS = {
    "article_citation": re.compile(
        r"\b(?:art|arts|artigo|artigos|article|articles|articulo|articulos)\b\.?\s*(?:n[o.]?\s*)?\d+(?:o|-[a-z])?"
        r"(?![\w])"
    ),
    "precedent_citation": re.compile(
        r"\b(?:resp|aresp|re|are|agrg|agint|hc|ms|adi|adpf|rr|airr)\b\s*(?:n[o.]?\s*)?\d[\d.]*(?:/[a-z]{2})?"
        r"|\bsumula\s+(?:vinculante\s+)?(?:n[o.]?\s*)?\d+|\btema\s+(?:n[o.]?\s*)?\d[\d.]*\d"
    ),
}


class DetectSpec(Protocol):
    @property
    def classes(self) -> Sequence[str]: ...
    @property
    def roles(self) -> Mapping[str, Sequence[str]]: ...
    @property
    def terms(self) -> Sequence[str]: ...
    @property
    def patterns(self) -> Sequence[str]: ...
    @property
    def document_sections(self) -> Sequence[str]: ...


class ValueEvidenceSpec(Protocol):
    @property
    def same_role(self) -> bool: ...
    @property
    def fresh_for(self) -> str | None: ...
    @property
    def type(self) -> str | None: ...
    @property
    def value(self) -> str | None: ...
    @property
    def must_state_gaps(self) -> bool: ...
    @property
    def gap_terms(self) -> Mapping[str, Sequence[str]]: ...


class AnchorEvidenceSpec(Protocol):
    @property
    def min_match(self) -> float: ...


class EvidenceSpec(Protocol):
    @property
    def value(self) -> ValueEvidenceSpec | None: ...
    @property
    def tool(self) -> str | None: ...
    @property
    def tool_any(self) -> Sequence[str]: ...
    @property
    def anchor(self) -> AnchorEvidenceSpec | None: ...


class NaturesSpec(Protocol):
    @property
    def computed(self) -> str: ...
    @property
    def quoted(self) -> str: ...
    @property
    def model(self) -> str | None: ...


class ActionsSpec(Protocol):
    @property
    def default(self) -> str: ...
    @property
    def contexts(self) -> Mapping[str, str]: ...


class CategorySpec(Protocol):
    """A category of the contract: `niadra.models.state.ClaimCategory`, as the SDK profile serves it, or any
    object with the same fields."""

    @property
    def id(self) -> str: ...
    @property
    def agents(self) -> Sequence[str]: ...
    @property
    def detect(self) -> DetectSpec: ...
    @property
    def evidence(self) -> EvidenceSpec: ...
    @property
    def natures(self) -> NaturesSpec: ...
    @property
    def actions(self) -> ActionsSpec: ...


@dataclass(frozen=True, slots=True)
class TurnValue:
    """A value with provenance the turn holds: a field of a tool's result or of a state read."""

    cls: str
    value: Mapping[str, str]
    """As `Mention.value()` writes it."""
    role: str | None = None
    fresh: bool = True
    object_type: str | None = None
    name: str | None = None
    """The field or computed value it is."""
    call_id: str | None = None
    ref: str | None = None
    declared_gaps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Anchor:
    """A passage an output cites, as the tool or the agent emitted it: where, what it quotes, from what."""

    start: int
    end: int
    quote: str
    document: str


@dataclass(frozen=True, slots=True)
class Turn:
    values: tuple[TurnValue, ...] = ()
    tools: tuple[str, ...] = ()
    """The tools called in the turn."""
    documents: Mapping[str, str] = field(default_factory=dict)
    """The documents in hand, by id: what quotes and anchors are checked against."""
    anchors: tuple[Anchor, ...] = ()
    sections: Mapping[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    """The output's named sections, as spans, when it is a document."""


@dataclass(frozen=True, slots=True)
class Finding:
    category: str
    start: int
    end: int
    verdict: str
    action: str
    cls: str | None = None
    nature: str | None = None
    role: str | None = None
    value: Mapping[str, str] | None = None
    evidence: TurnValue | Anchor | None = None


@dataclass(frozen=True, slots=True)
class Output:
    text: str
    lang: str
    context: str
    immutable: bool
    agent: str | None = None


def _date_parts(iso: str) -> tuple[int | None, int | None, int | None]:
    if iso.startswith("---"):
        return None, None, int(iso[3:])
    if iso.startswith("--"):
        month, day = iso[2:].split("-")
        return None, int(month), int(day)
    parts = [int(p) for p in iso.split("-")]
    return parts[0], parts[1], parts[2] if len(parts) == 3 else None


def _same_date(a: str | None, b: str | None) -> bool:
    """Equal on every part both state, and both state a day or neither does: "5 de outubro" is the 5th of
    October of any year, never "outubro de 2026"."""
    if a is None or b is None:
        return a == b
    pa, pb = _date_parts(a), _date_parts(b)
    if (pa[2] is None) != (pb[2] is None):
        return False
    return all(x == y for x, y in zip(pa, pb, strict=True) if x is not None and y is not None)


def same_value(a: Mapping[str, str], b: Mapping[str, str]) -> bool:
    """The same number: equal amounts (or ends of a range), or the same date; a unit only counts when both
    have one, so "511,06" is the R$ 511,06 of a tool's result."""
    if a.get("unit") and b.get("unit") and a["unit"] != b["unit"]:
        return False
    if set(a) - {"unit"} != set(b) - {"unit"}:
        return False
    if "date" in a:
        return _same_date(a["date"], b["date"])
    if "date_from" in a:
        return _same_date(a["date_from"], b["date_from"]) and _same_date(a["date_to"], b["date_to"])
    return all(Decimal(a[k]) == Decimal(b[k]) for k in ("amount", "min", "max") if k in a)


def _in_quotes(text: str, start: int, end: int) -> tuple[int, int] | None:
    return next(((s, e) for s, e in quotations(text) if s <= start and end <= e), None)


def _term_hits(text: str, terms: Sequence[str]) -> list[tuple[int, int]]:
    found = words(text)
    hits = set()
    for phrase in {as_words(t) for t in terms}:
        for i in range(len(found)):
            if phrase and phrase_at(found, i, phrase):
                hits.add((found[i].start, found[i + len(phrase) - 1].end))
    return sorted(hits)


def _numbers(category: CategorySpec, output: Output) -> list[Mention]:
    detect = category.detect
    found = [m for m in mentions(output.text, output.lang) if m.cls in detect.classes]
    if not detect.terms:
        return found
    hits = [start for start, _ in _term_hits(output.text, detect.terms)]
    spans = [sentence_of(output.text, m.start) for m in found]
    return [m for m, (low, high) in zip(found, spans, strict=True) if any(low <= h < high for h in hits)]


def _stated(gap: str, output: Output, gap_terms: Mapping[str, Sequence[str]]) -> bool:
    """A declared gap is stated by one of its words, or by its name when the contract gives none."""
    terms = gap_terms.get(gap) or (gap.replace("_", " "),)
    return bool(_term_hits(output.text, terms))


def _settle(category: CategorySpec, output: Output, verdict: str, chosen: str | None = None) -> str:
    if verdict in STANDING:
        return "none"
    if verdict == "not_checked":
        return "count"
    chosen = chosen or category.actions.contexts.get(output.context, category.actions.default)
    return _RECORDED.get(chosen, chosen)


def _rewrite(output: Output, mention: Mention, turn: Turn, stale: TurnValue) -> TurnValue | None:
    """The value a stale number is rewritten to, when that is unequivocal: a mutable output, a point value of
    a class that may change, a literal copy of one field of one object whose fresh value differs, and no
    other number of the class in the sentence (a derived one would go wrong). None otherwise."""
    low, high = sentence_of(output.text, mention.start)
    others = [m for m in mentions(output.text, output.lang) if m.cls == mention.cls and low <= m.start < high]
    same_field = [v for v in turn.values if v.cls == stale.cls and (v.ref, v.name) == (stale.ref, stale.name)]
    fresh = [v for v in same_field if v.fresh and not same_value(v.value, stale.value)]
    single = stale.ref is not None and stale.name is not None and len(fresh) == 1
    if output.immutable or mention.cls not in REWRITABLE or mention.is_range or len(others) > 1 or not single:
        return None
    return fresh[0]


@dataclass(frozen=True, slots=True)
class _Judged:
    nature: str
    verdict: str
    evidence: TurnValue | None = None
    chosen: str | None = None
    """An action that overrides the category's: what its natures give to a number the model said."""


def nature_of(text: str, mention: Mention, role: Role, values: Sequence[TurnValue]) -> str:
    """`quoted` inside quotation marks; `computed` when the turn holds a value of the number's class with its
    role or its value; `model` otherwise."""
    if _in_quotes(text, mention.start, mention.end) is not None:
        return "quoted"
    value = mention.value()
    of_class = [v for v in values if v.cls == mention.cls]
    if any(
        (role.name is not None and v.role == role.name) or (value and same_value(v.value, value))
        for v in of_class
    ):
        return "computed"
    return "model"


def _judge(category: CategorySpec, output: Output, turn: Turn, mention: Mention, role: Role) -> _Judged:
    value = mention.value()
    assert value is not None
    of_class = [v for v in turn.values if v.cls == mention.cls]
    nature = nature_of(output.text, mention, role, turn.values)
    if nature == "quoted":
        quote = _in_quotes(output.text, mention.start, mention.end)
        assert quote is not None
        if category.natures.quoted == "count":
            return _Judged("quoted", "not_checked")
        passage = " ".join(output.text[quote[0] : quote[1]].split()).lower()
        found = any(passage in " ".join(doc.split()).lower() for doc in turn.documents.values())
        return _Judged("quoted", "quoted_found" if found else "quoted_missing")
    by_role = [v for v in of_class if role.name is not None and v.role == role.name]
    spec = category.evidence.value
    if spec is None:
        return _Judged(nature, _tools_verdict(category, turn))
    if nature == "model":
        return _Judged("model", "unsupported", chosen=category.natures.model)
    if category.natures.computed == "count":
        return _Judged("computed", "not_checked")
    if role.status == "ambiguous":
        return _Judged("computed", "role_ambiguous")
    # A number with no word of a role is checked against every value of its class: only a named role narrows.
    candidates = by_role if spec.same_role and role.name is not None else of_class
    if spec.type is not None:
        candidates = [v for v in candidates if v.object_type == spec.type and v.name in (spec.value, None)]
    matched = next((v for v in candidates if same_value(v.value, value)), None)
    if matched is None:
        return _Judged("computed", "mismatch" if candidates else "no_evidence")
    if spec.fresh_for == "claim" and not matched.fresh:
        return _Judged("computed", "stale", matched)
    unsaid = [gap for gap in matched.declared_gaps if not _stated(gap, output, spec.gap_terms)]
    if spec.must_state_gaps and unsaid:
        return _Judged("computed", "gap_not_stated", matched)
    return _Judged("computed", "matched", matched)


def _number(category: CategorySpec, output: Output, turn: Turn, mention: Mention, role: Role) -> Finding:
    judged = _judge(category, output, turn, mention, role)
    action = _settle(category, output, judged.verdict, judged.chosen)
    evidence = judged.evidence
    if action == "rewrite":
        fresh = _rewrite(output, mention, turn, evidence) if judged.verdict == "stale" and evidence else None
        # The evidence of a rewrite is the fresh value the number becomes.
        action, evidence = ("rewrite", fresh) if fresh is not None else ("warn", evidence)
    return Finding(
        category.id,
        mention.start,
        mention.end,
        judged.verdict,
        action,
        mention.cls,
        judged.nature,
        role.name,
        mention.value(),
        evidence,
    )


def _tools_verdict(category: CategorySpec, turn: Turn) -> str:
    needed = [category.evidence.tool] if category.evidence.tool else list(category.evidence.tool_any)
    return "matched" if any(tool in turn.tools for tool in needed) else "no_evidence"


def _plain(category: CategorySpec, output: Output, turn: Turn, start: int, end: int) -> Finding:
    """A claim that is no number: a term, a citation, a sentence of a document section."""
    anchor_spec = category.evidence.anchor
    if anchor_spec is None:
        verdict = _tools_verdict(category, turn)
        return Finding(category.id, start, end, verdict, _settle(category, output, verdict))
    anchor = next((a for a in turn.anchors if a.start < end and start < a.end), None)
    if anchor is None:
        return Finding(category.id, start, end, "not_checked", "count")
    document = turn.documents.get(anchor.document)
    if document is None:
        verdict = "source_missing"
    else:
        verdict = "anchored" if score(anchor.quote, document) >= anchor_spec.min_match else "below_threshold"
    return Finding(category.id, start, end, verdict, _settle(category, output, verdict), evidence=anchor)


def check(categories: Sequence[CategorySpec], output: Output, turn: Turn) -> list[Finding]:
    """Every claim of `output` that a category detects, in the contract's order and then the text's."""
    findings: list[Finding] = []
    folded = fold(output.text)
    said = [m for m in mentions(output.text, output.lang) if m.cls != "label"]
    unasserted = hedged(output.text, said)
    for category in categories:
        if category.agents and output.agent not in category.agents:
            continue
        detect = category.detect
        if detect.classes:
            numbers = _numbers(category, output)
            # Numbers of one class compete for the words around them; a date and a duration never do.
            roles: dict[Mention, Role] = {}
            for cls in {m.cls for m in numbers}:
                same = [m for m in numbers if m.cls == cls]
                roles.update(zip(same, roles_of(output.text, same, detect.roles), strict=True))
            findings += [_number(category, output, turn, m, roles[m]) for m in numbers if m not in unasserted]
        elif detect.terms:
            for start, end in _term_hits(output.text, detect.terms):
                findings.append(_plain(category, output, turn, start, end))
        for name in detect.patterns:
            for m in PATTERNS[name].finditer(folded):
                findings.append(_plain(category, output, turn, m.start(), m.end()))
        for section in detect.document_sections:
            for low, high in turn.sections.get(section, ()):
                for start, end in sentences(output.text):
                    if low <= start and end <= high and output.text[start:end].strip():
                        findings.append(_plain(category, output, turn, start, end))
    return findings


def detected(categories: Sequence[CategorySpec], output: Output) -> list[str]:
    """The categories that detect anything in `output`: what a phrase of the negative corpus must never
    trigger."""
    found = check(categories, output, Turn())
    return sorted({f.category for f in found})
