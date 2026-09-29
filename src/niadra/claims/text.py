"""Offsets, words and sentences of an output, as every part of the checker reads them."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache


def fold(text: str) -> str:
    """Lower case without accents, one character for each character, so an offset in the folded text is an
    offset in the text: a character that folds to more than one (`ß`) becomes `?`."""
    if text.isascii():
        return text.lower()
    out = []
    for ch in text:
        plain = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)).lower()
        out.append(plain if len(plain) == 1 else "?")
    return "".join(out)


_WORD = re.compile(r"[a-z0-9]+(?:['\u2019][a-z]+)?")


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    """Folded."""
    start: int
    end: int


@lru_cache(maxsize=256)
def words(text: str) -> tuple[Word, ...]:
    """The folded words and numbers of a text, with their offsets; punctuation separates them."""
    return tuple(Word(m.group(), m.start(), m.end()) for m in _WORD.finditer(fold(text)))


def phrase_at(found: tuple[Word, ...], i: int, phrase: tuple[str, ...]) -> bool:
    return tuple(w.text for w in found[i : i + len(phrase)]) == phrase


def as_words(term: str) -> tuple[str, ...]:
    return tuple(w.text for w in words(term))


# A sentence ends at `!`, `?` or `;` before a space, at a period before a space and a capital letter (so
# "art. 5" and "R$ 1.234,56" never end one), and at a line break.
_BOUNDARY = re.compile(r"[!?;](?=\s|$)|\.(?=\s+[A-ZÀ-Ý]|\s*$)|\n")


@lru_cache(maxsize=256)
def sentences(text: str) -> tuple[tuple[int, int], ...]:
    spans = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        if m.end() > start:
            spans.append((start, m.end()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return tuple(spans)


def sentence_of(text: str, offset: int) -> tuple[int, int]:
    return next((s for s in sentences(text) if s[0] <= offset < s[1]), (0, len(text)))


_QUOTES = {'"': '"', "“": "”", "«": "»", "„": "“"}


@lru_cache(maxsize=256)
def quotations(text: str) -> tuple[tuple[int, int], ...]:
    """The spans between quotation marks, marks excluded: straight double quotes pair in order, and the curly
    and angle quotes pair with their closing mark. A mark left open quotes nothing."""
    spans = []
    i = 0
    while i < len(text):
        close = _QUOTES.get(text[i])
        end = text.find(close, i + 1) if close else -1
        if end > i + 1:
            spans.append((i + 1, end))
            i = end + 1
        else:
            i += 1
    return tuple(spans)
