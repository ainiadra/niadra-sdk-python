"""The claim checker beyond its vectors (`tests/test_vectors.py`): the text anchor's distance."""

from __future__ import annotations

import random

from niadra.claims import score
from niadra.claims.anchor import distance


def _by_table(pattern: str, text: str) -> int:
    """The same distance by the plain dynamic program: a passage may start and end anywhere in the text."""
    previous = list(range(len(pattern) + 1))
    best = len(pattern)
    for ch in text:
        current = [0]
        for i in range(1, len(pattern) + 1):
            current.append(min(previous[i] + 1, current[i - 1] + 1, previous[i - 1] + (pattern[i - 1] != ch)))
        best = min(best, current[-1])
        previous = current
    return best


def test_the_bit_parallel_distance_is_the_dynamic_program() -> None:
    rng = random.Random(186)
    for _ in range(500):
        pattern = "".join(rng.choices("abc ", k=rng.randint(1, 80)))
        text = "".join(rng.choices("abc ", k=rng.randint(0, 120)))
        assert distance(pattern, text) == _by_table(pattern, text), (pattern, text)


def test_a_quote_holds_whatever_the_case_accents_and_punctuation() -> None:
    assert score("CAUSAR DANO A OUTREM", "Aquele que... causar dano a outrem, ainda que") == 1.0
    assert score("", "anything") == 0.0
