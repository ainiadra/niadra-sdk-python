"""Normalization and whole-token matching for the deterministic scores, and the plain history render."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from niadra_bench.dataset.model import Case, Expectation

_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")
_NON_WORD = re.compile(r"[^0-9a-z]+")


def normalize(text: str) -> list[str]:
    """Lowercase, no accents, decimal commas as dots, everything else split into tokens.

    `R$ 37,90` and `$37.90` both become `... 37 90`, so an amount matches whichever separator the
    answer used; `01310-100` becomes `01310 100`.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _DECIMAL_COMMA.sub(".", text)
    return [token for token in _NON_WORD.split(text) if token]


def contains(haystack: str | list[str], needle: str) -> bool:
    """True when the needle's tokens appear in the haystack as a contiguous run of whole tokens."""
    tokens = normalize(haystack) if isinstance(haystack, str) else haystack
    wanted = normalize(needle)
    if not wanted:
        return False
    size = len(wanted)
    return any(tokens[i : i + size] == wanted for i in range(len(tokens) - size + 1))


def matches_all(text: str, groups: Iterable[Iterable[str]]) -> bool:
    tokens = normalize(text)
    return all(any(contains(tokens, alt) for alt in group) for group in groups)


def matches_none(text: str, forbidden: Iterable[str]) -> bool:
    tokens = normalize(text)
    return not any(contains(tokens, value) for value in forbidden)


def passes(text: str, expect: Expectation, *, verified: bool = False) -> bool:
    """The deterministic verdict on an answer (or a context): every expected group, no forbidden value."""
    groups = expect.verified_all_of if verified else expect.all_of
    if verified:
        return matches_all(text, groups)
    return matches_all(text, groups) and matches_none(text, expect.none_of)


def render_history(case: Case) -> str:
    """Every session in order, as plain lines: the reference for the validity rule and the gold system."""
    lines: list[str] = []
    for session in case.chronological():
        stamp = f"{session.days_ago:.1f} days ago, {session.channel}"
        if session.record is not None:
            lines.append(f"[{stamp}] {session.record.text}")
        for turn in session.turns:
            who = "customer" if turn.role == "customer" else "agent"
            lines.append(f"[{stamp}] {who}: {turn.text}")
    return "\n".join(lines)
