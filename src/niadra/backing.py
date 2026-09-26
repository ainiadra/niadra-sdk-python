"""Backed answers: no number, date, code or amount the agent says without a source.

Before an agent's answer is recorded, a rule reads the values it states and looks each one up in
what the agent had: the context pack (text, live turns, slots, delta), the customer's own words in
this conversation, what a human attendant said in it, the results of the actions it recorded and
of the tools it called. No model runs; a check costs a few milliseconds.

What counts as a value:

- `amount`: a number with a currency or a money word (`R$ 249,90`, `$1,200.00`, `30 euros`), any
  size;
- `date`: `18/09`, `18/09/2026`, `2026-09-18`, `18 de setembro`, `September 18`, `18 de septiembre`;
- `code`: letters with three or more digits (`PX-4471`, `AB12C34`);
- `number`: three or more digits (`81220`, `45778-204`).

Words are never values ("dois dias"), and neither are numbers of one or two digits ("2 dias"),
times of day, or a year on its own. An amount is also backed when it is the sum or the difference
of two backed amounts, the sum of three, or a backed amount times a count said in the answer or the
sources ("3 x R$ 83,30"). A number is backed by its last digits ("final 4471") when the source holds
the whole of it.

Guards (the lines a memory v2 server writes for a kind of value agents got wrong) are checked too:
an answer that states another value of the guarded kind, and never the guarded one, went against
the guard.

A value that looks like a card or a document number (the card check, a CPF or CNPJ check digit) is
never written back in a report: it shows as `[withheld:card]` or `[withheld:document]`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "BackingReport",
    "GuardSpec",
    "Sources",
    "UnbackedValue",
    "ValueKind",
    "check",
    "values_in",
    "violated",
]

ValueKind = Literal["amount", "date", "number", "code"]

MAX_VALUES = 100
"""Values read per answer at most; past them, the rest of a pasted table is not checked."""
MAX_SUM_TERMS = 64
"""Amounts combined into sums at most: the newest of the sources, enough for a bill and its lines."""
MAX_FACTOR = 60
"""A count an amount is multiplied by at most (installments)."""

_MONTHS = {
    # Portuguese, English and Spanish, folded (no accents), with the common short forms.
    **dict.fromkeys(("janeiro", "january", "enero", "jan", "ene"), 1),
    **dict.fromkeys(("fevereiro", "february", "febrero", "fev", "feb"), 2),
    **dict.fromkeys(("marco", "march", "marzo", "mar"), 3),
    **dict.fromkeys(("abril", "april", "abr", "apr"), 4),
    **dict.fromkeys(("maio", "may", "mayo", "mai"), 5),
    **dict.fromkeys(("junho", "june", "junio", "jun"), 6),
    **dict.fromkeys(("julho", "july", "julio", "jul"), 7),
    **dict.fromkeys(("agosto", "august", "ago", "aug"), 8),
    **dict.fromkeys(("setembro", "september", "septiembre", "setiembre", "set", "sep", "sept"), 9),
    **dict.fromkeys(("outubro", "october", "octubre", "out", "oct"), 10),
    **dict.fromkeys(("novembro", "november", "noviembre", "nov"), 11),
    **dict.fromkeys(("dezembro", "december", "diciembre", "dez", "dec", "dic"), 12),
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))

_CURRENCY = r"(?:r\$|us\$|u\$s|\$|€|£|usd|brl|eur|mxn|ars|clp|cop)"
_MONEY_WORD = r"(?:reais|real|d[oó]lares|dollars?|euros?|pesos?|brl|usd|eur)"
_NUM = r"\d[\d.,]*\d|\d"

_AMOUNT = re.compile(
    rf"(?<![\w$€£])(?:{_CURRENCY})\s?(?P<a>{_NUM})|(?P<b>{_NUM})\s?(?:{_MONEY_WORD})\b", re.IGNORECASE
)
_DATE_WORDS = re.compile(
    rf"\b(?P<d1>\d{{1,2}})(?:º|o|st|nd|rd|th)?\s+(?:de\s+|of\s+)?(?P<m1>{_MONTH})\b\.?"
    rf"(?:,?\s+(?:de\s+)?(?P<y1>\d{{4}}))?"
    rf"|\b(?P<m2>{_MONTH})\.?\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<y2>\d{{4}}))?",
    re.IGNORECASE,
)
_DATE_NUMERIC = re.compile(
    r"(?<![\w/.-])(?:(?P<y>\d{4})-(?P<ym>\d{1,2})-(?P<yd>\d{1,2})"
    r"|(?P<d>\d{1,2})(?P<sep>[/-])(?P<m>\d{1,2})(?:(?P=sep)(?P<year>\d{4}|\d{2}))?"
    r"|(?P<dd>\d{1,2})\.(?P<dm>\d{1,2})\.(?P<dy>\d{4}))(?![\w/-]|\.\d)"
)
_TIME = re.compile(r"(?<!\d)\d{1,2}[:h]\d{2}(?!\d)")
_GROUPED = re.compile(r"(?<![\d.,])\d{4}(?:[ -]\d{4}){2,3}(?:[ -]\d{1,3})?(?![\d])")
"""Digits written in groups of four, as cards are: read as one number, so the card check sees it."""
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,/-]*[A-Za-z0-9]|[0-9]")
_TRAILING = ".,/-"
_WORD = re.compile(r"[a-z]+")

_CARD_FIRST = "23456"


@dataclass(frozen=True, slots=True)
class UnbackedValue:
    """A value the answer states that no source backs."""

    kind: ValueKind
    value: str
    """As the answer writes it; `[withheld:card]` or `[withheld:document]` for what looks like one."""
    start: int
    """Where it starts in the answer."""

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class BackingReport:
    """What the check found in one answer."""

    checked: int
    unbacked: tuple[UnbackedValue, ...] = ()
    guard_violations: tuple[str, ...] = ()
    """Short ids of the guards the answer went against."""
    conflicting: tuple[UnbackedValue, ...] = ()
    """The values that went against a guard: another value of the guarded kind."""

    @property
    def ok(self) -> bool:
        return not self.unbacked and not self.guard_violations

    @property
    def problems(self) -> list[UnbackedValue]:
        """What `strict` returns: the values with no source, then those that went against a guard."""
        seen = {v.start for v in self.unbacked}
        return [*self.unbacked, *(v for v in self.conflicting if v.start not in seen)]

    def event_fields(self) -> dict[str, Any]:
        """The report as the agent's turn carries it: kinds, counts and ids, never a value."""
        return {
            "checked": min(self.checked, 500),
            "unbacked_values": [{"kind": v.kind} for v in self.unbacked[:MAX_VALUES]],
            "guard_violations": list(self.guard_violations[:8]),
        }

    def message(self) -> str:
        """One sentence for the agent to fix its answer by: which values have no source."""
        if not self.unbacked and not self.guard_violations:
            return ""
        parts = []
        if self.unbacked:
            said = ", ".join(v.value for v in self.unbacked)
            parts.append(f"These values are in no source you have: {said}.")
        if self.guard_violations:
            parts.append("The answer states a value other than one memory holds for the same kind.")
        return " ".join(parts) + " Rephrase without them, or ask."


@dataclass(frozen=True, slots=True)
class Found:
    kind: ValueKind
    raw: str
    start: int
    end: int
    key: str
    """Digits only for a number, letters and digits upper case for a code, cents for an amount,
    `d/m/y` for a date (`y` may be empty)."""


def _fold(text: str) -> str:
    """Lower case without accents, one character per character, so offsets still point into `text`."""
    if text.isascii():
        return text.lower()
    out = []
    for ch in text:
        plain = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        low = plain.lower()
        out.append(low if len(low) == 1 else "?")
    return "".join(out)


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def cents(raw: str) -> int | None:
    """An amount in cents, in either convention: `1.234,56`, `1,234.56`, `249,90`, `249.90`, `30`."""
    text = raw.strip().strip(_TRAILING)
    if not text or not text[0].isdigit():
        return None
    last = max(text.rfind(","), text.rfind("."))
    if last >= 0 and len(text) - last - 1 in (1, 2):
        whole, frac = text[:last], text[last + 1 :]
    else:
        whole, frac = text, ""
    whole = _digits(whole)
    if not whole or len(whole) > 15:
        return None
    return int(whole) * 100 + int((frac + "00")[:2] or 0)


def _date_key(day: int, month: int, year: int | None) -> str | None:
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    return f"{day}/{month}/{year if year is not None else ''}"


def _year(raw: str | None) -> int | None:
    if not raw:
        return None
    value = int(raw)
    return value + 2000 if value < 100 else value


def values_in(text: str, *, limit: int = MAX_VALUES) -> list[Found]:
    """Every value a text states, in order: amounts, dates, codes and numbers of three or more digits.
    A span taken by one is never read again as another."""
    folded = _fold(text)
    taken: list[tuple[int, int]] = []
    found: list[Found] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in taken)

    def take(item: Found) -> None:
        taken.append((item.start, item.end))
        found.append(item)

    for m in _AMOUNT.finditer(folded):
        group = "a" if m.group("a") else "b"
        value = cents(m.group(group))
        if value is not None and free(m.start(), m.end()):
            take(Found("amount", text[m.start() : m.end()].strip(), m.start(), m.end(), str(value)))
    for m in _DATE_WORDS.finditer(folded):
        day = int(m.group("d1") or m.group("d2"))
        month = _MONTHS[(m.group("m1") or m.group("m2")).lower()]
        key = _date_key(day, month, _year(m.group("y1") or m.group("y2")))
        if key is not None and free(m.start(), m.end()):
            take(Found("date", text[m.start() : m.end()], m.start(), m.end(), key))
    for m in _DATE_NUMERIC.finditer(folded):
        if m.group("y"):
            key = _date_key(int(m.group("yd")), int(m.group("ym")), int(m.group("y")))
        elif m.group("dd"):
            key = _date_key(int(m.group("dd")), int(m.group("dm")), int(m.group("dy")))
        else:
            day, month, year = int(m.group("d")), int(m.group("m")), _year(m.group("year"))
            # Day first, as PT and ES write it; an impossible month reads the date the US way.
            key = _date_key(day, month, year) or _date_key(month, day, year)
        if key is not None and free(m.start(), m.end()):
            take(Found("date", text[m.start() : m.end()], m.start(), m.end(), key))
    for m in _TIME.finditer(folded):
        if free(m.start(), m.end()):
            taken.append((m.start(), m.end()))  # a time of day is not checked
    for m in _GROUPED.finditer(text):
        if free(m.start(), m.end()):
            take(Found("number", m.group(0), m.start(), m.end(), _digits(m.group(0))))
    for m in _TOKEN.finditer(text):
        raw = m.group(0).rstrip(_TRAILING)
        start, end = m.start(), m.start() + len(raw)
        if not free(start, end):
            continue
        digits = _digits(raw)
        if len(digits) < 3:
            continue
        if any(ch.isalpha() for ch in raw):
            if raw[-2:].lower() in ("st", "nd", "rd", "th") and raw[:-2].isdigit():
                continue
            key = "".join(ch for ch in raw.upper() if ch.isalnum())
            take(Found("code", raw, start, end, key))
            continue
        if len(digits) == 4 and raw == digits and 1900 <= int(digits) <= 2099:
            continue  # a year on its own
        take(Found("number", raw, start, end, digits))
    found.sort(key=lambda f: f.start)
    return found[:limit]


class Sources:
    """What the agent had, indexed for the check: add each text once, check many answers. Texts are
    kept by value, so the same pack added every turn is indexed once."""

    def __init__(self) -> None:
        self.numbers: set[str] = set()
        self.amounts: set[int] = set()
        self._amount_order: list[int] = []
        self.dates: set[str] = set()
        self.codes: set[str] = set()
        self.counts: set[int] = set()
        self._seen: set[str] = set()

    def add(self, text: str | None) -> None:
        if not text or text in self._seen:
            return
        self._seen.add(text)
        for item in values_in(text, limit=10_000):
            self._index(item)
        # Every token with digits backs a number or an amount written another way ("249,90" backs
        # "R$ 249.90"), and small integers are counts an amount may be multiplied by.
        for m in _TOKEN.finditer(text):
            raw = m.group(0).rstrip(_TRAILING)
            digits = _digits(raw)
            if not digits:
                continue
            self.numbers.add(digits)
            if raw.isdigit() and 1 < int(raw) <= MAX_FACTOR:
                self.counts.add(int(raw))
            value = cents(raw)
            if value is not None:
                self._amount(value)
            if any(ch.isalpha() for ch in raw):
                self.codes.add("".join(ch for ch in raw.upper() if ch.isalnum()))

    def extend(self, texts: Iterable[str | None]) -> None:
        for text in texts:
            self.add(text)

    def _amount(self, value: int) -> None:
        if value not in self.amounts:
            self.amounts.add(value)
            self._amount_order.append(value)

    def _index(self, item: Found) -> None:
        if item.kind == "amount":
            self._amount(int(item.key))
        elif item.kind == "date":
            day, month, year = item.key.split("/")
            self.dates.add(item.key)
            self.dates.add(f"{day}/{month}/")
            if int(day) <= 12:
                self.dates.add(f"{month}/{day}/{year}")  # ambiguous: read both ways
                self.dates.add(f"{month}/{day}/")
        elif item.kind == "code":
            self.codes.add(item.key)
        else:
            self.numbers.add(item.key)

    def backs(self, item: Found, said: Sequence[Found] = ()) -> bool:
        if item.kind == "amount":
            return self._backs_amount(int(item.key), said)
        if item.kind == "date":
            day, month, _ = item.key.split("/")
            return item.key in self.dates or f"{day}/{month}/" in self.dates
        if item.kind == "code":
            return item.key in self.codes or _digits(item.key) in self.numbers
        if item.key in self.numbers:
            return True
        value = cents(item.raw)
        if value is not None and value in self.amounts:
            return True
        # "final 4471": the last digits of a number the sources hold whole.
        return len(item.key) >= 4 and any(
            n.endswith(item.key) for n in self.numbers if len(n) > len(item.key)
        )

    def _backs_amount(self, value: int, said: Sequence[Found]) -> bool:
        if value in self.amounts:
            return True
        terms = self._amount_order[-MAX_SUM_TERMS:]
        pool = set(terms)
        for a in terms:
            if value - a in pool or a - value in pool:
                return True  # a + b, or a - b
        for i, a in enumerate(terms):
            for b in terms[i + 1 :]:
                if value - a - b in pool:
                    return True
        counts = set(self.counts) | {int(f.key) for f in said if f.kind == "number" and f.key.isdigit()}
        counts |= {int(m) for m in re.findall(r"(?<![\d.,])(\d{1,2})\s?x\b", " ".join(f.raw for f in said))}
        return any(1 < n <= MAX_FACTOR and value % n == 0 and value // n in pool for n in counts)


@dataclass(frozen=True, slots=True)
class GuardSpec:
    id: str
    value_type: str
    value: str


_TYPE_STEMS: dict[str, tuple[str, ...]] = {
    "protocol": ("protocol",),
    "ticket": ("chamado", "ticket", "ocorrenc", "incident", "incidenc", "caso", "case"),
    "order": ("pedido", "order", "orden", "compra", "purchase"),
    "record": ("registr", "record", "sinistro", "claim", "siniestro"),
    "receipt": ("comprovante", "receipt", "recibo", "comprobante", "transac"),
    "postal_code": ("cep", "zip", "postal"),
    "code": ("codigo", "code", "cupom", "coupon", "cupon", "voucher", "rastrei", "tracking", "reserva"),
}
_NEAR = 6


def _typed_near(answer: str, found: Sequence[Found], value_type: str) -> list[Found]:
    """The numbers and codes of the answer at most six words from a word naming `value_type`
    ("o protocolo do seu atendimento é 81221")."""
    stems = _TYPE_STEMS.get(value_type, ())
    if not stems:
        return []
    starts = [m.start() for m in _WORD.finditer(_fold(answer))]
    named = [m.start() for m in _WORD.finditer(_fold(answer)) if m.group(0).startswith(stems)]
    near = []
    for item in found:
        if item.kind not in ("number", "code"):
            continue
        for at in named:
            low, high = sorted((at, item.start))
            if sum(1 for w in starts if low <= w < high) <= _NEAR:
                near.append(item)
                break
    return near


def _guard_value(spec: GuardSpec) -> Found | None:
    found = values_in(spec.value)
    if found:
        return found[0]
    digits = _digits(spec.value)
    return Found("number", spec.value, 0, len(spec.value), digits) if len(digits) >= 3 else None


def _amount_of(item: Found) -> int | None:
    return int(item.key) if item.kind == "amount" else cents(item.raw)


def _same(a: Found, b: Found) -> bool:
    """Two writings of one value: the same day and month (a missing year matches any year), the same
    amount, or the same digits (`45778-204` and `45778204`)."""
    if a.kind == "date" and b.kind == "date":
        da, ma, ya = a.key.split("/")
        db, mb, yb = b.key.split("/")
        return (da, ma) == (db, mb) and (not ya or not yb or ya == yb)
    if "amount" in (a.kind, b.kind):
        left, right = _amount_of(a), _amount_of(b)
        return left is not None and left == right
    if "code" in (a.kind, b.kind):
        return a.key == b.key or _digits(a.key) == _digits(b.key)
    return a.key == b.key


def conflicts(spec: GuardSpec, answer: str, found: Sequence[Found] | None = None) -> list[Found]:
    """The values of the guarded kind the answer states, when none of them is the guarded one: the
    answer went against the guard. Empty when it did not."""
    guarded = _guard_value(spec)
    if guarded is None:
        return []
    said = list(found) if found is not None else values_in(answer)
    if spec.value_type == "date":
        of_type = [f for f in said if f.kind == "date"]
    elif spec.value_type == "amount":
        of_type = [f for f in said if f.kind == "amount"]
    else:
        of_type = _typed_near(answer, said, spec.value_type)
    return [] if any(_same(f, guarded) for f in said) else of_type


def violated(spec: GuardSpec, answer: str, found: Sequence[Found] | None = None) -> bool:
    """The answer states another value of the guarded kind and never the guarded one."""
    return bool(conflicts(spec, answer, found))


def _masked(item: Found) -> str:
    digits = _digits(item.raw)
    only_digits = all(ch.isdigit() or ch in " .-/" for ch in item.raw)
    if only_digits and _card(digits):
        return "[withheld:card]"
    if only_digits and ((len(digits) == 11 and _cpf(digits)) or (len(digits) == 14 and _cnpj(digits))):
        return "[withheld:document]"
    return item.raw


def _card(number: str) -> bool:
    if not (13 <= len(number) <= 19) or number[0] not in _CARD_FIRST:
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _mod11(values: Sequence[int], weights: Iterable[int]) -> int:
    rest = sum(v * w for v, w in zip(values, weights, strict=False)) % 11
    return 0 if rest < 2 else 11 - rest


def _cpf(number: str) -> bool:
    d = [int(c) for c in number]
    if len(set(d)) == 1:
        return False
    first = _mod11(d[:9], range(10, 1, -1))
    return d[9:] == [first, _mod11([*d[:9], first], range(11, 1, -1))]


def _cnpj(number: str) -> bool:
    d = [int(c) for c in number]
    if len(set(d)) == 1:
        return False
    first = _mod11(d[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    second = _mod11([*d[:12], first], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return d[12:] == [first, second]


def check(
    answer: str,
    sources: Sources | Iterable[str | None],
    guards: Iterable[Any] = (),
) -> BackingReport:
    """Reads the values `answer` states and looks each up in `sources`; checks it against `guards`
    (`PackGuard`s, or mappings with `id`, `value_type` and `value`)."""
    index = sources if isinstance(sources, Sources) else _index(sources)
    said = values_in(answer)
    unbacked = tuple(
        UnbackedValue(item.kind, _masked(item), item.start) for item in said if not index.backs(item, said)
    )
    violations: list[str] = []
    conflicting: dict[int, UnbackedValue] = {}
    for spec in (_spec(g) for g in guards):
        if spec is None:
            continue
        found = conflicts(spec, answer, said)
        if found:
            violations.append(spec.id)
            for item in found:
                conflicting.setdefault(item.start, UnbackedValue(item.kind, _masked(item), item.start))
    return BackingReport(len(said), unbacked, tuple(dict.fromkeys(violations)), tuple(conflicting.values()))


def _index(texts: Iterable[str | None]) -> Sources:
    index = Sources()
    index.extend(texts)
    return index


def _spec(guard: Any) -> GuardSpec | None:
    def get(name: str) -> Any:
        return guard.get(name) if isinstance(guard, dict) else getattr(guard, name, None)

    ident, value_type, value = get("id"), get("value_type"), get("value")
    if not (isinstance(ident, str) and isinstance(value_type, str) and isinstance(value, str)):
        return None
    return GuardSpec(ident, value_type, value)
