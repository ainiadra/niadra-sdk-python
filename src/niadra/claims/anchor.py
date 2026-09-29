"""The text anchor (the claim contract spec, section 9): how closely a quoted passage matches the document it
cites.

Both texts are normalized the same way: lower case, no accents, every run of anything but letters and digits
one space, trimmed. The score is `1 - d / len(quote)`, where `d` is the fewest insertions, deletions and
substitutions that turn the quote into some passage of the document (any start, any end), and an anchor holds
at 0.90 or above. The distance runs as Myers' bit-parallel algorithm, linear in the document's length.
"""

from __future__ import annotations

import re
import unicodedata

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return _NOT_ALNUM.sub(" ", plain.lower()).strip()


def distance(pattern: str, text: str) -> int:
    """The edit distance between `pattern` and the passage of `text` nearest to it."""
    m = len(pattern)
    if m == 0:
        return 0
    full = (1 << m) - 1
    top = 1 << (m - 1)
    peq: dict[str, int] = {}
    for i, ch in enumerate(pattern):
        peq[ch] = peq.get(ch, 0) | (1 << i)
    pv, mv, score = full, 0, m
    best = m
    for ch in text:
        eq = peq.get(ch, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | (~(xh | pv) & full)
        mh = pv & xh
        if ph & top:
            score += 1
        elif mh & top:
            score -= 1
        # A passage may start anywhere in the text: no carry into the first row.
        ph = (ph << 1) & full
        mh = (mh << 1) & full
        pv = mh | (~(xv | ph) & full)
        mv = ph & xv
        best = min(best, score)
    return best


def score(quote: str, document: str) -> float:
    """1.0 when the normalized quote is a passage of the normalized document; 0.0 for an empty quote."""
    q, d = normalize(quote), normalize(document)
    if not q:
        return 0.0
    if q in d:
        return 1.0
    return 1 - distance(q, d) / len(q)
