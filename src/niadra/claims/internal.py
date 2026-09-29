"""Internal text (the claim contract spec, section 11): fingerprints of the company's own prompt, so an output
that repeats a passage of it gives way to the contract's `redact` line.

```python
niadra.internal_text.register("prompts@v16", CORE_PROMPT)  # the contract's `shingle_hashes_ref`
```

A fingerprint is the SHA-256 of `n` consecutive words of the prompt (folded as the claim checker folds text,
joined by one space), and the prompt never leaves the process: Niadra knows only the name of the version
the fingerprints come from. `shingles()` computes them apart, for a company that ships only the fingerprints
to the agent's process (`register_hashes()`).

Each passage found becomes one claim of the turn record: category `internal_text`, verdict
`internal_text_found`, the span it had before the redaction, and the prompt version as its evidence.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable

from niadra.claims.text import words

CATEGORY = "internal_text"
VERDICT = "internal_text_found"


def shingles(text: str, n: int) -> set[str]:
    """The fingerprints of every `n` consecutive words of `text`."""
    folded = [w.text for w in words(text)]
    return {_hash(folded[i : i + n]) for i in range(len(folded) - n + 1)}


class InternalText:
    """`niadra.internal_text`: the company's prompt fingerprints by version, held in the process only."""

    def __init__(self) -> None:
        self._texts: dict[str, list[list[str]]] = {}
        self._hashes: dict[tuple[str, int], set[str]] = {}
        self._lock = threading.Lock()

    def register(self, ref: str, *texts: str) -> None:
        """The prompt texts of version `ref` (the contract's `shingle_hashes_ref`)."""
        with self._lock:
            self._texts.setdefault(ref, []).extend([w.text for w in words(t)] for t in texts)
            for key in [k for k in self._hashes if k[0] == ref]:
                del self._hashes[key]

    def register_hashes(self, ref: str, hashes: Iterable[str], *, n: int = 8) -> None:
        """Fingerprints computed apart with `shingles(text, n)`."""
        with self._lock:
            self._hashes.setdefault((ref, n), set()).update(hashes)

    def __contains__(self, ref: str) -> bool:
        with self._lock:
            return ref in self._texts or any(k[0] == ref for k in self._hashes)

    def passages(self, text: str, ref: str, n: int) -> list[tuple[int, int]]:
        """Where `text` repeats `n` words of the prompt `ref`, as merged spans of code points."""
        known = self._fingerprints(ref, n)
        if not known:
            return []
        found = words(text)
        spans: list[tuple[int, int]] = []
        for i in range(len(found) - n + 1):
            if _hash([w.text for w in found[i : i + n]]) in known:
                start, end = found[i].start, found[i + n - 1].end
                if spans and start <= spans[-1][1]:
                    spans[-1] = (spans[-1][0], max(end, spans[-1][1]))
                else:
                    spans.append((start, end))
        return spans

    def _fingerprints(self, ref: str, n: int) -> set[str]:
        with self._lock:
            known = self._hashes.get((ref, n))
            if known is None and ref in self._texts:
                known = {
                    _hash(folded[i : i + n])
                    for folded in self._texts[ref]
                    for i in range(len(folded) - n + 1)
                }
                self._hashes[(ref, n)] = known
            return known or set()


def record(span: tuple[int, int], ref: str, act: str) -> dict[str, object]:
    """A passage as the turn record's `claims` carries it: never its text."""
    return {
        "category": CATEGORY,
        "span": [span[0], span[1]],
        "verdict": VERDICT,
        "action": act,
        "evidence": {"document": ref},
    }


def _hash(folded: list[str]) -> str:
    return hashlib.sha256(" ".join(folded).encode()).hexdigest()
