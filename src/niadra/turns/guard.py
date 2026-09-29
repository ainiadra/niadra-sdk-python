"""The claim contract that acts: at the stream bridge, before the customer, or on a whole output before it
goes (`spec/claim-contract.md`, sections 8 and 12).

```python
async for chunk in conversation.claims.guard(stream):  # the model's stream, text chunks
    await sse.send(chunk)
guarded = conversation.claims.guard_text(reply)  # a whole message, or a document before it is saved
```

Each claim of the output gets its verdict against what the turn holds, and the category's action for the
output's context decides what happens, as the turn record's `claims` then says:

- `block`: in a mutable output, the sentence gives way to the category's `replace_with` (once per message;
  a later blocked sentence is dropped), or is dropped without one; in an immutable output nothing changes
  and the whole output goes to a person (`Guarded.review`);
- `rewrite_if_unequivocal`: the number becomes the fresh value of the one field it copies, written the way
  the output wrote it, only when that is unequivocal (the spec's 8.3) and the output is mutable; otherwise
  the claim is marked `warn`;
- `warn`: the text goes as it is and the claim is marked; `count`: only measured; `discard_anchor`: the
  anchor is dropped and counted.

**In a stream,** text flows through untouched until something could start a claim: a digit, a currency, a
quotation mark, or the first word of one of the contract's terms or citation patterns. From there the guard
holds the text until the sentence ends, checks it and lets it go as the actions leave it. Where a context
blocks, a sentence is held from its start, so what gives way to the caveat is the whole sentence. A hold
lasts at most `hold` seconds (150 ms) and a message's holds `message` seconds in total (300 ms): past them the
text goes as it is, the turn is flagged `guard_budget_exceeded`, and its claims are recorded as marked. The
guard never replaces an answer with an error: a check that fails lets the text through.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from niadra.claims import Finding, TurnValue, mentions
from niadra.claims.check import PATTERNS
from niadra.claims.numbers import to_decimal
from niadra.claims.text import as_words, fold
from niadra.models.state import ClaimCategory, ClaimContractSummary
from niadra.models.turns import ClaimRecord
from niadra.turns.capture import Said, TurnFrame
from niadra.turns.claims import findings_of, record_of

logger = logging.getLogger("niadra")

HOLD = 0.150
"""Seconds one hold may last."""
MESSAGE = 0.300
"""Seconds a message's holds may add up to."""
ACTED = frozenset({"block", "rewrite", "warn"})
"""Acts that make the turn `guard_acted`."""

_BOUNDARY = re.compile(r"[!?;](?=\s)|\.(?=\s+[A-ZÀ-Ý])|\n")
_TRAILING_WORD = re.compile(r"[\w$€£]+$")
_NUMBER = re.compile(r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})([/.-])(\d{1,2})(?:\2(\d{2}|\d{4}))?$")


@dataclass
class Guarded:
    """An output after the guard: the text as it may go, the claims recorded for it, and whether it must go to
    a person first (a block in an immutable output)."""

    text: str
    claims: list[ClaimRecord] = field(default_factory=list)
    review: bool = False

    @property
    def changed(self) -> bool:
        """Whether a block or a rewrite changed the text."""
        return any(c.action in ("block", "rewrite") for c in self.claims) and not self.review


class Guard:
    """The acting check of one output. `feed()` takes text as it arrives and returns what may go now,
    `expire()` lets a hold that ran out go, and `finish()` returns the rest and records the claims in the
    turn; `result` is then the whole output as it went."""

    def __init__(
        self,
        contract: ClaimContractSummary,
        frame: TurnFrame | None,
        *,
        context: str = "chat",
        immutable: bool | None = None,
        agent: str | None = None,
        hold: float = HOLD,
        message: float = MESSAGE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.contract = contract
        self.frame = frame
        self.context = context
        self.immutable = context in contract.outputs.immutable if immutable is None else immutable
        self.agent = agent or (frame.agent if frame is not None else None)
        self._hold = hold
        self._message = message
        self._clock = clock
        self._categories = [c for c in contract.categories if not c.agents or self.agent in c.agents]
        self._candidate = _candidates(self._categories)
        # Where a context blocks, text goes sentence by sentence: a blocked claim takes its whole sentence.
        self._whole = not self.immutable and any(_blocks(c, context) for c in self._categories)
        self.text = ""
        self._out: list[str] = []
        self._released = 0
        self._held_at: float | None = None
        self._held_total = 0.0
        self._exceeded = False
        self._unchecked: list[tuple[int, int]] = []
        self._caveat_sent = False
        self._records: list[dict[str, Any]] = []
        self._review = False
        self.result: Guarded | None = None

    def remaining(self) -> float:
        """Seconds the current hold may still last; infinite while nothing is held."""
        if self._held_at is None:
            return float("inf")
        spent = self._clock() - self._held_at
        return max(0.0, min(self._hold - spent, self._message - self._held_total - spent))

    def feed(self, chunk: str) -> str:
        self.text += chunk
        return self._advance(final=False)

    def expire(self) -> str:
        """The hold ran out: what it held goes as it is."""
        if self._held_at is None:
            return ""
        return self._let_go(len(self.text) - len(_trailing(self.text)))

    def finish(self) -> str:
        """The output ended: the rest is checked and goes, and the claims go to the turn."""
        tail = self._advance(final=True)
        if self._unchecked:
            self._record_unchecked()
        text = "".join(self._out)
        if self.frame is not None:
            self.frame.add_claims(self._records)
            if any(r["action"] in ACTED for r in self._records):
                self.frame.flag("guard_acted")
            if self._exceeded:
                self.frame.flag("guard_budget_exceeded")
            self.frame.guarded(text)
        self.result = Guarded(text, [ClaimRecord.model_validate(r) for r in self._records], self._review)
        return tail

    def _advance(self, *, final: bool) -> str:
        out: list[str] = []
        while True:
            safe = len(self.text) if final else len(self.text) - len(_trailing(self.text))
            if self._held_total >= self._message:  # the message's holds are spent: the rest goes as it is
                out.append(self._let_go(safe))
                return "".join(out)
            if self._held_at is not None and not final and self.remaining() <= 0:
                out.append(self._let_go(safe))
                continue
            start = self._candidate_at(self._released, safe)
            if self._whole:
                boundary = safe if final else _last_closed(self.text, self._released, safe)
                if boundary is None:
                    if start is not None and self._held_at is None:
                        self._held_at = self._clock()
                    return "".join(out)
                ends_sentence = start is not None and start < boundary
                out.append(self._decide(boundary) if ends_sentence else self._pass(boundary))
                if final or boundary >= safe:
                    return "".join(out)
                continue
            if start is None:
                out.append(self._pass(safe))
                return "".join(out)
            out.append(self._pass(start))
            if self._held_at is None:
                self._held_at = self._clock()
            closed = safe if final else _closed(self.text, start)
            if closed is None:
                return "".join(out)
            out.append(self._decide(closed))
            if final:
                return "".join(out)

    def _pass(self, upto: int) -> str:
        """Text with nothing to check goes as it is."""
        if upto <= self._released:
            return ""
        piece = self.text[self._released : upto]
        self._released = upto
        self._out.append(piece)
        return piece

    def _let_go(self, upto: int) -> str:
        """A hold ran out: the held text goes unchecked, and its claims are recorded as marked."""
        self._stop_holding()
        self._exceeded = True
        if upto > self._released:
            self._unchecked.append((self._released, upto))
        return self._pass(upto)

    def _stop_holding(self) -> None:
        if self._held_at is not None:
            self._held_total += self._clock() - self._held_at
            self._held_at = None

    def _candidate_at(self, low: int, high: int) -> int | None:
        if high <= low:
            return None
        found = self._candidate.search(fold(self.text), low, high)
        return found.start() if found is not None else None

    def _decide(self, upto: int) -> str:
        """Checks the held text, up to the end of its sentence, and lets it go as the actions leave it."""
        self._stop_holding()
        low = self._released
        text = self.text[:upto]
        piece = text[low:]
        try:
            findings = [f for f in self._findings(text) if low <= f.start < upto]
            piece = self._act(text, low, findings)
        except Exception:
            logger.warning("niadra: the claim guard failed; the text goes as it is", exc_info=True)
            if self.frame is not None:
                self.frame.incomplete()
        self._released = upto
        self._out.append(piece)
        return piece

    def _findings(self, text: str) -> list[Finding]:
        return findings_of(self.frame, self.contract, Said(text, self.context, self.immutable, self.agent))

    def _act(self, text: str, low: int, findings: list[Finding]) -> str:
        rewrites: dict[tuple[int, int], str] = {}
        blocked: dict[tuple[int, int], str | None] = {}
        """Each blocked sentence, with the caveat of the first of its categories that has one."""
        for finding in findings:
            act = finding.action
            if act == "rewrite":
                written = None if self.immutable else _rewritten(text, finding, self.contract)
                if written is None:
                    act = "warn"
                else:
                    rewrites[(finding.start, finding.end)] = written
            elif act == "block":
                if self.immutable:
                    self._review = True
                else:
                    span = self._sentence(text, finding.start, low)
                    blocked[span] = blocked.get(span) or self._caveat(finding.category)
            if (record := record_of(finding, act)) is not None:
                self._records.append(record)
        edits = [(s, e, t) for (s, e), t in rewrites.items() if not any(b <= s < c for b, c in blocked)]
        for (start, end), caveat in sorted(blocked.items()):
            # The sentence goes with the space before it; the space after it stays for the next one.
            body = text[start:end]
            lead = body[: len(body) - len(body.lstrip())]
            end = start + len(body.rstrip())
            if caveat is not None and not self._caveat_sent:
                self._caveat_sent = True
                edits.append((start, end, lead + caveat))
            else:
                edits.append((start, end, ""))
        piece = text[low:]
        for start, end, new in sorted(edits, reverse=True):
            piece = piece[: start - low] + new + piece[end - low :]
        return piece

    @staticmethod
    def _sentence(text: str, offset: int, low: int) -> tuple[int, int]:
        """The sentence around `offset`, split as the stream is: at the ends the text after them confirms."""
        start, end = low, len(text)
        for found in _BOUNDARY.finditer(text, low):
            if found.end() > offset:
                end = found.end()
                break
            start = found.end()
        return start, end

    def _caveat(self, category: str) -> str | None:
        found = next((c for c in self._categories if c.id == category), None)
        return found.actions.replace_with if found is not None else None

    def _record_unchecked(self) -> None:
        """The claims of text a hold let go: checked now, and recorded as what happened to them."""
        try:
            findings = self._findings(self.text)
        except Exception:
            return
        for finding in findings:
            if any(s <= finding.start < e for s, e in self._unchecked):
                act = finding.action if finding.action in ("none", "count", "discard_anchor") else "warn"
                if (record := record_of(finding, act)) is not None:
                    self._records.append(record)


def guard_text(
    contract: ClaimContractSummary,
    frame: TurnFrame | None,
    text: str,
    *,
    context: str = "chat",
    immutable: bool | None = None,
    agent: str | None = None,
) -> Guarded:
    """The guard on a whole output: nothing is held, so nothing goes unchecked."""
    guard = Guard(contract, frame, context=context, immutable=immutable, agent=agent)
    guard.text = text
    guard.finish()
    assert guard.result is not None
    return guard.result


def guard_stream(guard: Guard, stream: Iterable[str]) -> Iterator[str]:
    """A synchronous stream through the guard: a hold ends when a chunk arrives past its budget."""
    for chunk in stream:
        if piece := guard.feed(chunk):
            yield piece
    if tail := guard.finish():
        yield tail


async def guard_async_stream(guard: Guard, stream: AsyncIterable[str]) -> AsyncIterator[str]:
    """An asynchronous stream through the guard: a hold that runs out lets its text go without waiting for
    the next chunk."""
    chunks = aiter(stream)
    pending: asyncio.Future[str] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(chunks))
            wait = guard.remaining()
            done, _ = await asyncio.wait({pending}, timeout=None if wait == float("inf") else wait)
            if not done:
                if piece := guard.expire():
                    yield piece
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                break
            finally:
                if pending.done():
                    pending = None
            if piece := guard.feed(chunk):
                yield piece
    finally:
        if pending is not None:
            pending.cancel()
    if tail := guard.finish():
        yield tail


def _trailing(text: str) -> str:
    """A word that may still grow: what a candidate term or number could be the start of."""
    found = _TRAILING_WORD.search(text)
    return found.group() if found is not None else ""


def _closed(text: str, offset: int) -> int | None:
    """Where the sentence holding `offset` ends, once the text that follows confirms it; None until then."""
    found = _BOUNDARY.search(text, offset)
    return found.end() if found is not None else None


def _last_closed(text: str, low: int, high: int) -> int | None:
    """The end of the last sentence between `low` and `high` that the text after it confirms."""
    ends = [m.end() for m in _BOUNDARY.finditer(text, low) if m.end() <= high]
    return ends[-1] if ends else None


def _blocks(category: ClaimCategory, context: str) -> bool:
    configured = category.actions.contexts.get(context, category.actions.default)
    return configured == "block" or category.natures.model == "block"


def _candidates(categories: list[ClaimCategory]) -> re.Pattern[str]:
    """What could start a claim of these categories, in folded text."""
    alternatives: list[str] = []
    if any(c.detect.classes for c in categories):
        alternatives += [r"\d", r"r\$", r"us\$", r"[$€£]", '["“«„]']
    firsts = {
        words[0]
        for c in categories
        if not c.detect.classes
        for term in c.detect.terms
        if (words := as_words(term))
    }
    keywords = {
        w
        for c in categories
        for name in c.detect.patterns
        for w in re.findall(r"[a-z]{2,}", PATTERNS[name].pattern.split(r"\b")[1])
    }
    if firsts or keywords:
        alternatives.append(r"\b(?:" + "|".join(sorted(map(re.escape, firsts | keywords))) + r")\b")
    return re.compile("|".join(alternatives) or r"(?!)")


def _rewritten(text: str, finding: Finding, contract: ClaimContractSummary) -> str | None:
    """The number of a rewrite, written with the fresh value the way the output wrote the old one; None when
    that cannot be done without doubt (a number in words, a scale, another unit, a date in words)."""
    fresh = finding.evidence
    if not isinstance(fresh, TurnValue):
        return None
    lang = contract.languages[0] if contract.languages else "pt"
    span = text[finding.start : finding.end]
    mention = next(
        (m for m in mentions(text, lang) if (m.start, m.end) == (finding.start, finding.end)), None
    )
    if mention is None or mention.written != "digits" or mention.is_range:
        return None
    if mention.date is not None and "date" in fresh.value:
        return _date(span, fresh.value["date"], lang)
    if mention.amount is None or "amount" not in fresh.value:
        return None
    if mention.unit and fresh.value.get("unit") and mention.unit != fresh.value["unit"]:
        return None
    number = _NUMBER.search(span)
    if number is None or to_decimal(number.group(), lang) != mention.amount:
        return None
    try:
        new = _amount(Decimal(fresh.value["amount"]), number.group(), lang)
    except InvalidOperation:
        return None
    return None if new is None else span[: number.start()] + new + span[number.end() :]


def _amount(value: Decimal, raw: str, lang: str) -> str | None:
    """`value` with the marks and the decimals of `raw`."""
    marks = [ch for ch in raw if ch in ".,"]
    grouping_mark = "," if lang == "en" else "."
    decimal_mark = "." if lang == "en" else ","
    decimals = 0
    grouped = False
    if marks:
        last = max(raw.rfind("."), raw.rfind(","))
        tail = len(raw) - last - 1
        if len(set(marks)) == 2 or tail != 3 or marks[-1] != grouping_mark:
            decimal_mark, decimals = raw[last], tail
            grouping_mark = "," if decimal_mark == "." else "."
            grouped = len(marks) > 1
        else:
            grouped = True
    if value != value.quantize(Decimal(1).scaleb(-decimals)):
        if decimals or value != value.quantize(Decimal("0.01")):
            return None
        decimals = 2
    whole, _, fraction = f"{value.quantize(Decimal(1).scaleb(-decimals)):f}".partition(".")
    if grouped:
        whole = f"{int(whole):,}".replace(",", grouping_mark)
    return whole + (decimal_mark + fraction if decimals else "")


def _date(span: str, iso: str, lang: str) -> str | None:
    """A numeric date with the fresh day, in the same shape; a date in words is never rewritten."""
    shape = _NUMERIC_DATE.match(span.strip())
    parts = iso.split("-")
    if shape is None or len(parts) != 3 or iso.startswith("-"):
        return None
    year, month, day = parts
    first, sep, second, old_year = shape.groups()
    day_first = lang != "en"
    a, b = (day, month) if day_first else (month, day)
    out = f"{a.zfill(len(first))}{sep}{b.zfill(len(second))}"
    if old_year is not None:
        out += sep + (year[-2:] if len(old_year) == 2 else year)
    return span.replace(span.strip(), out)
