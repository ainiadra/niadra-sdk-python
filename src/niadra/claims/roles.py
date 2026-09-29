"""A number's role, from the words around it (the claim contract spec, section 6): the same R$ 511,06 is a
full price, a discounted one or a price per person by what the sentence says next to it.

A role's term counts within `WINDOW` words of the number, in the same sentence, and belongs to the nearest
number of the same class (to both when two are as near). The nearest term gives the role, and at the same
distance a term of the number's own beats one it shares; two terms of different roles still tied leave the
number without one, `ambiguous`, which is never approved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from niadra.claims.numbers import Mention
from niadra.claims.text import as_words, phrase_at, sentence_of, words

WINDOW = 6
"""Words between a number and a term of its role, at most."""


@dataclass(frozen=True, slots=True)
class Role:
    name: str | None
    status: str
    """`matched`, `ambiguous` or `none`."""


@dataclass(frozen=True, slots=True)
class _Term:
    role: str
    first: int
    past: int
    """Word indexes: the term's first word, and the one after its last."""


def _terms(text: str, roles: Mapping[str, Sequence[str]], inside: Sequence[tuple[int, int]]) -> list[_Term]:
    found = words(text)
    hits = [
        _Term(role, i, i + len(phrase))
        for role, terms in roles.items()
        for phrase in {as_words(term) for term in terms}
        for i in range(len(found))
        if phrase and phrase_at(found, i, phrase)
    ]
    # The longest term at the leftmost place wins; a term inside a number's span is part of the number.
    kept: list[_Term] = []
    for hit in sorted(hits, key=lambda h: (h.first, -(h.past - h.first), h.role)):
        start, end = found[hit.first].start, found[hit.past - 1].end
        if any(s < end and start < e for s, e in inside):
            continue
        if all(hit.past <= k.first or hit.first >= k.past for k in kept) or any(
            (k.first, k.past) == (hit.first, hit.past) for k in kept
        ):
            kept.append(hit)
    return kept


def roles_of(text: str, numbers: Sequence[Mention], roles: Mapping[str, Sequence[str]]) -> list[Role]:
    """The role of each of `numbers` (a category's mentions, in order), by the terms of `roles`."""
    if not roles:
        return [Role(None, "none") for _ in numbers]
    found = words(text)
    spans = []
    for m in numbers:
        indexes = [i for i, w in enumerate(found) if m.start <= w.start < m.end]
        spans.append((indexes[0], indexes[-1] + 1) if indexes else (0, 0))
    sentences = [sentence_of(text, m.start) for m in numbers]

    def distance(term: _Term, k: int) -> int | None:
        first, past = spans[k]
        start = found[term.first].start
        if not sentences[k][0] <= start < sentences[k][1] or past == 0:
            return None
        gap = term.first - past if term.first >= past else first - term.past
        return gap if 0 <= gap <= WINDOW else None

    # Per number: (distance, shared with another number, role). A term of its own beats a shared one at the
    # same distance: in "de R$ 299,90 por R$ 199,90", "por" is as near to both, and "de" is the first's alone.
    near: list[list[tuple[int, bool, str]]] = [[] for _ in numbers]
    for term in _terms(text, roles, [(m.start, m.end) for m in numbers]):
        distances = [(d, k) for k in range(len(numbers)) if (d := distance(term, k)) is not None]
        if not distances:
            continue
        closest = min(d for d, _ in distances)
        owners = [k for d, k in distances if d == closest]
        for k in owners:
            near[k].append((closest, len(owners) > 1, term.role))
    out = []
    for terms in near:
        if not terms:
            out.append(Role(None, "none"))
            continue
        best = min((d, shared) for d, shared, _ in terms)
        named = {role for d, shared, role in terms if (d, shared) == best}
        out.append(Role(named.pop(), "matched") if len(named) == 1 else Role(None, "ambiguous"))
    return out
