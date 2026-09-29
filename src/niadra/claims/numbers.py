"""The numbers of an output, read by rules (the claim contract spec, section 5): each one with its class, its
normalized value and its span, in Portuguese, English or Spanish.

Every number of the text lands in at most one mention, found in this order, and a later step never takes
what an earlier one took:

1. labels by shape: a case number, a postal code, a tax id, a phone, a time of day;
2. labels by the word before them: an order, a protocol, a statute's article, a size, a list position;
3. dates: ISO, numeric (day first in `pt` and `es`, month first in `en`), with month names, a month of a
   year, "dia 20";
4. amounts with what follows or precedes them: money (a currency before or after, a scale like "mil"),
   percent, duration, dose, quantity, installments ("10x"); written in digits, or in words right before
   the unit; two of them joined by "a", "to", "-" (or "e", "and", "y" after "entre", "between") make a
   range;
5. codes that mix letters and digits, and ordinals: labels;
6. the rest: a number with two decimals is money; an integer before a word is a count; five digits or more,
   a leading zero or a year are labels; anything else is not read.

A label is never a claim and is never rewritten.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache

from niadra.claims.text import fold, words
from niadra.claims.words import read_words

LANGUAGES = ("pt", "en", "es")

_NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?(?![\d])|\d+(?:[.,]\d+)?"
_NUMBER = re.compile(rf"(?<![\w.,/])(?<![a-z]-)(?:{_NUM})(?![.,]?\d)")


@dataclass(frozen=True, slots=True)
class Mention:
    cls: str
    """`money`, `percent`, `date`, `duration`, `quantity`, `count`, `dosage` or `label`."""
    start: int
    end: int
    amount: Decimal | None = None
    low: Decimal | None = None
    high: Decimal | None = None
    unit: str | None = None
    date: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    written: str = "digits"

    def value(self) -> dict[str, str] | None:
        """The normalized value, as the vectors and the turn record write it; a label has none."""
        if self.cls == "label":
            return None
        if self.date is not None:
            return {"date": self.date}
        if self.date_from is not None and self.date_to is not None:
            return {"date_from": self.date_from, "date_to": self.date_to}
        out = (
            {"amount": decimal_text(self.amount)}
            if self.amount is not None
            else {"min": decimal_text(self.low), "max": decimal_text(self.high)}
        )
        if self.unit is not None:
            out["unit"] = self.unit
        return out

    @property
    def is_range(self) -> bool:
        return self.low is not None or self.date_from is not None


def decimal_text(value: Decimal | None) -> str:
    assert value is not None
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def to_decimal(raw: str, lang: str) -> Decimal | None:
    """A number in either convention: the last separator of two kinds is the decimal one; a separator that
    repeats groups thousands; a single one before exactly three digits groups them when it is the language's
    grouping mark (`.` in `pt` and `es`, `,` in `en`) and is the decimal mark otherwise."""
    seps = [ch for ch in raw if ch in ".,"]
    if not seps:
        return Decimal(raw)
    decimal_at = None
    if len(set(seps)) == 2:
        decimal_at = max(raw.rfind("."), raw.rfind(","))
    elif len(seps) == 1:
        at = raw.find(seps[0])
        grouping = "," if lang == "en" else "."
        if len(raw) - at - 1 != 3 or seps[0] != grouping:
            decimal_at = at
    whole, frac = (raw, "") if decimal_at is None else (raw[:decimal_at], raw[decimal_at + 1 :])
    groups = re.split(r"[.,]", whole)
    if len(groups) > 1 and (not 1 <= len(groups[0]) <= 3 or any(len(g) != 3 for g in groups[1:])):
        return None
    return Decimal("".join(groups) + ("." + frac if frac else ""))


_MONTHS = {
    **dict.fromkeys(("janeiro", "january", "enero"), 1),
    **dict.fromkeys(("fevereiro", "february", "febrero"), 2),
    **dict.fromkeys(("marco", "march", "marzo"), 3),
    **dict.fromkeys(("abril", "april"), 4),
    **dict.fromkeys(("maio", "may", "mayo"), 5),
    **dict.fromkeys(("junho", "june", "junio"), 6),
    **dict.fromkeys(("julho", "july", "julio"), 7),
    **dict.fromkeys(("agosto", "august"), 8),
    **dict.fromkeys(("setembro", "september", "septiembre", "setiembre"), 9),
    **dict.fromkeys(("outubro", "october", "octubre"), 10),
    **dict.fromkeys(("novembro", "november", "noviembre"), 11),
    **dict.fromkeys(("dezembro", "december", "diciembre"), 12),
}
_SHORT_MONTHS = {
    **dict.fromkeys(("jan", "ene"), 1),
    **dict.fromkeys(("fev", "feb"), 2),
    **dict.fromkeys(("mar",), 3),
    **dict.fromkeys(("abr", "apr"), 4),
    **dict.fromkeys(("mai",), 5),
    **dict.fromkeys(("jun",), 6),
    **dict.fromkeys(("jul",), 7),
    **dict.fromkeys(("ago", "aug"), 8),
    **dict.fromkeys(("set", "sep", "sept"), 9),
    **dict.fromkeys(("out", "oct"), 10),
    **dict.fromkeys(("nov",), 11),
    **dict.fromkeys(("dez", "dec", "dic"), 12),
}
# A short month name counts only with its period ("5 set. 2026"): "mar", "set" and "out" are also words.
_MONTH = "|".join(
    [
        *sorted(_MONTHS, key=len, reverse=True),
        *(rf"{m}\." for m in sorted(_SHORT_MONTHS, key=len, reverse=True)),
    ]
)
_ORD = r"(?:o|a|st|nd|rd|th)?"
_SEP = r"\s*(?:-|\u2013|\u2014|\ba\b|\bao\b|\bal\b|\bate\b|\bto\b|\bhasta\b)\s*"
_BETWEEN = re.compile(r"(?:entre|between)\s+$")
_AND = re.compile(r"\s+(?:e|and|y)\s+")


def _month(name: str) -> int:
    return _MONTHS.get(name) or _SHORT_MONTHS[name.rstrip(".")]


def _iso(year: int | None, month: int | None, day: int | None) -> str | None:
    if month is not None and not 1 <= month <= 12:
        return None
    if day is not None:
        try:
            date(year or 2024, month or 1, day)  # a leap year when the text gives none: "29 de fevereiro"
        except ValueError:
            return None
    if year is not None and month is not None and day is not None:
        return f"{year:04d}-{month:02d}-{day:02d}"
    if year is not None and month is not None:
        return f"{year:04d}-{month:02d}"
    if month is not None and day is not None:
        return f"--{month:02d}-{day:02d}"
    return f"---{day:02d}" if day is not None else None


def _year(raw: str | None) -> int | None:
    if raw is None:
        return None
    return 2000 + int(raw) if len(raw) == 2 else int(raw)


# Labels by shape.
_SHAPES = re.compile(
    r"(?<![\w.])(?:"
    r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}"  # a case number (CNJ)
    r"|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2}"  # tax ids
    r"|\d{5}-\d{3}"  # a postal code
    r"|\(?\d{2}\)?\s?9?\d{4}-\d{4}|\+\d{1,3}(?:[\s-]?\d{2,5}){2,4}"  # a phone
    r"|\d{1,2}:\d{2}(?::\d{2})?(?:\s?(?:am|pm))?|\d{1,2}h\d{2}|\d{1,2}\s?(?:am|pm)\b"  # a time of day
    r")(?![\w-])"
    r"|(?<=\bas\s)\d{1,2}h\b|(?<=\bat\s)\d{1,2}(?:h\b|\b)|(?<=\blas\s)\d{1,2}(?:h\b|\b)"  # "às 14h", "at 3"
)
_CUES = (
    "pedido|order|orden|protocolo|protocol|numero|nr|processo|proceso|case|cep|zip|art|arts|artigo|artigos|"
    "article|articulo|inciso|paragrafo|lei|law|ley|decreto|sumula|tema|tamanho|tam|size|talla|numeracao|item|"
    "itens|opcao|option|opcion|posicao|position|pagina|page|pag|versao|version|capitulo|chapter|clausula|"
    "clause|apto|apartamento|sala|vara|nf|cupom|coupon|codigo|code|cpf|cnpj|rg|rastreio|tracking|chamado|"
    "ticket|sinistro|apolice|poliza|matricula|ref|referencia|resp|aresp|agrg|agint|adi|adpf|hc|rr|airr"
)
"""Words after which a number names something instead of measuring it. Only number markers ("nº", "#", ":")
may stand between: "pedido de 3 itens" is a count."""
_FILLER = r"(?:\s*(?:n[o.]?|numero|nr\.?|#|:)\s*)*"
_CUED = re.compile(rf"\b(?:{_CUES})\b\.?{_FILLER}\s*(?P<n>\d[\d./-]*\d[a-z]?|\d[a-z]?)(?![\w])")
_LAW_SIGN = re.compile(r"§\s*(?P<n>\d+)")


def _labels_by_shape(text: str) -> list[Mention]:
    return [Mention("label", m.start(), m.end()) for m in _SHAPES.finditer(text)]


def _labels_by_cue(text: str) -> list[Mention]:
    found = [Mention("label", m.start("n"), m.end("n")) for m in _CUED.finditer(text)]
    return found + [Mention("label", m.start("n"), m.end("n")) for m in _LAW_SIGN.finditer(text)]


_ISO_DATE = re.compile(r"(?<![\w.-])(\d{4})-(\d{2})-(\d{2})(?![\w-])")
_SLASH_DATE = re.compile(r"(?<![\w/.-])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\w/]|[.-]\d)")
_DOTTED_DATE = re.compile(r"(?<![\w/.-])(\d{1,2})([.-])(\d{1,2})\2(\d{4})(?![\w/.-])")
_MONTH_YEAR_NUM = re.compile(r"(?<![\w/.-])(\d{1,2})/(\d{4})(?![\w/])")
_DAY_MONTH = re.compile(
    rf"(?<![\w.,])(\d{{1,2}}){_ORD}(?:{_SEP}(\d{{1,2}}){_ORD})?\s+(?:de\s+|of\s+|del\s+)?({_MONTH})(?![\w])"
    rf"(?:,?\s+(?:de\s+|del\s+)?(\d{{4}}))?(?![\w])"
)
_MONTH_DAY = re.compile(rf"\b({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?(?![\w])")
_MONTH_OF_YEAR = re.compile(rf"\b({_MONTH})\s+(?:de\s+|of\s+|del\s+)?(\d{{4}})(?![\w])")
_DAY_ONLY = re.compile(r"\b(?:dia|day)\s+(\d{1,2})(?:o|a|st|nd|rd|th)?(?![\w%]|[.,]\d)")


def _dates(text: str, lang: str) -> list[Mention]:
    found: list[Mention] = []

    def add(start: int, end: int, iso: str | None) -> None:
        if iso is not None:
            found.append(Mention("date", start, end, date=iso))

    for m in _ISO_DATE.finditer(text):
        add(m.start(), m.end(), _iso(int(m[1]), int(m[2]), int(m[3])))
    for m in _SLASH_DATE.finditer(text):
        first, second = int(m[1]), int(m[2])
        day, month = (second, first) if lang == "en" else (first, second)
        add(m.start(), m.end(), _iso(_year(m[3]), month, day))
    for m in _DOTTED_DATE.finditer(text):
        first, second = int(m[1]), int(m[3])
        day, month = (second, first) if lang == "en" else (first, second)
        add(m.start(), m.end(), _iso(int(m[4]), month, day))
    for m in _MONTH_YEAR_NUM.finditer(text):
        add(m.start(), m.end(), _iso(int(m[2]), int(m[1]), None))
    for m in _DAY_MONTH.finditer(text):
        month, year = _month(m[3]), _year(m[4])
        if m[2] is None:
            add(m.start(), m.end(), _iso(year, month, int(m[1])))
            continue
        low, high = _iso(year, month, int(m[1])), _iso(year, month, int(m[2]))
        if low is not None and high is not None and low < high:
            found.append(Mention("date", m.start(), m.end(), date_from=low, date_to=high))
    for m in _MONTH_DAY.finditer(text):
        add(m.start(), m.end(), _iso(_year(m[3]), _month(m[1]), int(m[2])))
    for m in _MONTH_OF_YEAR.finditer(text):
        if m[1] in _MONTHS:
            add(m.start(), m.end(), _iso(int(m[2]), _month(m[1]), None))
    for m in _DAY_ONLY.finditer(text):
        add(m.start(1), m.end(), _iso(None, None, int(m[1])))
    return found


_CURRENCY_BEFORE = re.compile(r"(?:(?<![a-z])(?:r\$|us\$|u\$s|usd|brl|eur|gbp|mxn|ars|clp|cop)|€|£|\$)\s?$")
_CURRENCIES = {
    "r$": "BRL", "us$": "USD", "u$s": "USD", "usd": "USD", "brl": "BRL", "eur": "EUR", "€": "EUR",
    "gbp": "GBP", "£": "GBP", "mxn": "MXN", "ars": "ARS", "clp": "CLP", "cop": "COP", "$": None,
}  # fmt: skip
_SCALE = re.compile(r"\s?(mil|milhao|milhoes|millon|millones|million|millions|thousand|k)\b")
_SCALES = {"mil": 1000, "thousand": 1000, "k": 1000}
Suffix = tuple[str, str | None]
_SUFFIXES: list[tuple[re.Pattern[str], Suffix]] = [
    (re.compile(p), s)
    for p, s in (
        (r"\s?(?:%|por\s?cento\b|percent\b|per\s?cent\b|por\s?ciento\b)", ("percent", "%")),
        (r"\s?(?:reais|real)\b", ("money", "BRL")),
        (r"\s?(?:dolares|dolar|dollars|dollar)\b", ("money", "USD")),
        (r"\s?(?:euros|euro)\b", ("money", "EUR")),
        (r"\s?(?:pesos|peso)\b", ("money", None)),
        (r"\s?(?:brl)\b", ("money", "BRL")),
        (r"\s?(?:usd)\b", ("money", "USD")),
        (r"\s?(?:eur)\b", ("money", "EUR")),
        (
            r"\s?(?:dias?\s+uteis|dia\s+util|business\s+days?|working\s+days?|dias?\s+habiles|dia\s+habil)\b",
            ("duration", "business_day"),
        ),
        (
            r"\s?(?:dias?\s+corridos|calendar\s+days?|dias?\s+naturales|dias?\s+calendario)\b",
            ("duration", "day"),
        ),
        (r"\s?(?:dias?|days?)\b", ("duration", "day")),
        (r"\s?(?:horas?|hours?|hrs?|h)\b", ("duration", "hour")),
        (r"\s?(?:minutos?|minutes?|mins?)\b", ("duration", "minute")),
        (r"\s?(?:semanas?|weeks?)\b", ("duration", "week")),
        (r"\s?(?:meses|mes|months?)\b", ("duration", "month")),
        (r"\s?(?:anos?|years?)\b", ("duration", "year")),
        (r"\s?(?:mcg|ug|μg)\b", ("dosage", "mcg")),
        (r"\s?mg\b", ("dosage", "mg")),
        (r"\s?(?:ui|iu)\b", ("dosage", "IU")),
        (r"\s?(?:gotas?|drops?)\b", ("dosage", "drop")),
        (r"\s?(?:comprimidos?|tablets?|pastillas?)\b", ("dosage", "tablet")),
        (r"\s?(?:capsulas?|capsules?)\b", ("dosage", "capsule")),
        (r"\s?kg\b", ("quantity", "kg")),
        (r"\s?(?:ml)\b", ("quantity", "ml")),
        (r"\s?(?:litros?|liters?|litres?|l)\b", ("quantity", "l")),
        (r"\s?(?:gb)\b", ("quantity", "GB")),
        (r"\s?(?:mb)\b", ("quantity", "MB")),
        (r"\s?(?:tb)\b", ("quantity", "TB")),
        (r"\s?(?:mah)\b", ("quantity", "mAh")),
        (r"\s?(?:cm)\b", ("quantity", "cm")),
        (r"\s?(?:mm)\b", ("quantity", "mm")),
        (r"\s?(?:km)\b", ("quantity", "km")),
        (r"\s?(?:m2|m²)", ("quantity", "m2")),
        (r"\s?(?:metros?|meters?|metres?|m)\b", ("quantity", "m")),
        (r"\s?g\b", ("quantity", "g")),
        (r"\s?(?:w)\b", ("quantity", "W")),
        (r"\s?(?:v)\b", ("quantity", "V")),
        (r"\s?(?:polegadas?|pulgadas?|inches|inch)\b", ("quantity", "in")),
        (r"\s?(?:unidades?|units?|unidad|pecas?|pieces?|piezas?|itens|items?)\b", ("quantity", "unit")),
        (r"\s?(?:pares|pairs?)\b", ("quantity", "pair")),
        (r"x\b", ("count", "x")),
    )
]


@dataclass(frozen=True, slots=True)
class _Atom:
    raw: str
    start: int
    end: int
    """Past the number, its scale and its suffix."""
    amount: Decimal
    written: str
    scaled: bool
    suffix: Suffix | None
    prefix_start: int | None
    """Where a currency before it starts."""
    currency: str | None


def _suffix(text: str, at: int) -> tuple[Suffix, int] | None:
    for pattern, suffix in _SUFFIXES:
        if m := pattern.match(text, at):
            return suffix, m.end()
    return None


def _atom(text: str, raw: str, start: int, end: int, amount: Decimal, written: str) -> _Atom:
    prefix_start = currency = None
    if before := _CURRENCY_BEFORE.search(text, max(0, start - 5), start):
        prefix_start, currency = before.start(), _CURRENCIES[before.group().strip()]
    scaled = False
    if (scale := _SCALE.match(text, end)) is not None:
        after = _suffix(text, scale.end())
        # "5k" is money only: with a currency before or after it.
        if scale[1] != "k" or prefix_start is not None or (after is not None and after[0][0] == "money"):
            amount, end, scaled = amount * _SCALES.get(scale[1], 1_000_000), scale.end(), True
    found = _suffix(text, end)
    suffix = found[0] if found is not None else None
    if suffix is not None and suffix[0] != "money":
        prefix_start = currency = None  # "R$ 5 dias" is five days: the unit decides
    end = found[1] if found is not None else end
    return _Atom(raw, start, end, amount, written, scaled, suffix, prefix_start, currency)


def _atoms(text: str, lang: str, free: Callable[[int, int], bool]) -> list[_Atom]:
    atoms = []
    for m in _NUMBER.finditer(text):
        amount = to_decimal(m.group(), lang)
        if amount is not None and free(m.start(), m.end()):
            atoms.append(_atom(text, m.group(), m.start(), m.end(), amount, "digits"))
    found = words(text)
    i = 0
    while i < len(found):
        read = read_words(lang, found, i)
        if read is None:
            i += 1
            continue
        amount, past = read
        start, end = found[i].start, found[past - 1].end
        if free(start, end):
            atom = _atom(text, text[start:end], start, end, amount, "words")
            if atom.suffix is not None and atom.suffix[0] != "count":
                atoms.append(atom)
        i = past
    return sorted(atoms, key=lambda a: a.start)


def _class_of(atom: _Atom) -> Suffix | None:
    if atom.suffix is not None:
        return atom.suffix
    if atom.prefix_start is not None:
        return ("money", atom.currency)
    return None


_TWO_DECIMALS = re.compile(r"[.,]\d{2}$")
_NEXT_WORD = re.compile(r"\s+[a-z]")


def _bare(text: str, atom: _Atom) -> Mention | None:
    """A number with nothing around it that says what it measures."""
    raw = atom.raw
    if atom.written == "words":
        return None
    year = len(raw) == 4 and raw.isdigit() and 1900 <= int(raw) <= 2099
    if not atom.scaled and raw.isdigit() and (len(raw) >= 5 or (len(raw) > 1 and raw[0] == "0") or year):
        return Mention("label", atom.start, atom.end)
    if not atom.scaled and _TWO_DECIMALS.search(raw):
        return Mention("money", atom.start, atom.end, amount=atom.amount)
    if atom.amount == atom.amount.to_integral_value() and _NEXT_WORD.match(text, atom.end):
        return Mention("count", atom.start, atom.end, amount=atom.amount)
    return None


def _amounts(text: str, lang: str, free: Callable[[int, int], bool]) -> tuple[list[Mention], list[Mention]]:
    """The amounts a unit or a currency qualifies, and apart the bare numbers, which come after the codes."""
    atoms = _atoms(text, lang, free)
    found: list[Mention] = []
    rest: list[Mention] = []
    skip = False
    for i, atom in enumerate(atoms):
        nxt = atoms[i + 1] if i + 1 < len(atoms) else None
        if skip:
            skip = False
            continue
        kind = _class_of(atom)
        start = atom.prefix_start if atom.prefix_start is not None else atom.start
        # A range: this amount, a joiner, the next amount; the unit may come only once, after the second.
        other = _class_of(nxt) if nxt is not None else None
        if other is None and kind is not None and nxt is not None and nxt.written == atom.written:
            other = kind  # "R$ 300 a 400": the second amount, alone, takes the first one's unit
        if nxt is not None and other is not None and kind in (None, other) and atom.amount < nxt.amount:
            gap = text[atom.end : nxt.prefix_start if nxt.prefix_start is not None else nxt.start]
            between = _BETWEEN.search(text, max(0, start - 10), start) is not None
            if re.fullmatch(_SEP, gap) or (between and _AND.fullmatch(gap)):
                cls, unit = other
                found.append(Mention(cls, start, nxt.end, low=atom.amount, high=nxt.amount, unit=unit))
                skip = True
                continue
        if kind is not None:
            cls, unit = kind
            found.append(Mention(cls, start, atom.end, amount=atom.amount, unit=unit, written=atom.written))
        elif (bare := _bare(text, atom)) is not None:
            rest.append(bare)
    return found, rest


_CODE = re.compile(r"(?<![\w$])(?=[\w-]*\d)(?=[\w-]*[a-z])[a-z0-9]+(?:-[a-z0-9]+)*(?![\w])")


def _codes(text: str) -> list[Mention]:
    return [Mention("label", m.start(), m.end()) for m in _CODE.finditer(text)]


@lru_cache(maxsize=512)
def mentions(text: str, lang: str) -> tuple[Mention, ...]:
    """The numbers of `text` in the language `lang`, in the order they appear; offsets are code points."""
    if lang not in LANGUAGES:
        raise ValueError(f"unknown language {lang!r}")
    folded = fold(text)
    taken: list[Mention] = []

    def free(start: int, end: int) -> bool:
        return all(end <= t.start or start >= t.end for t in taken)

    def keep(candidates: list[Mention]) -> None:
        for mention in sorted(candidates, key=lambda c: (c.start, -(c.end - c.start))):
            if free(mention.start, mention.end):
                taken.append(mention)

    keep(_labels_by_shape(folded))
    keep(_labels_by_cue(folded))
    keep(_dates(folded, lang))
    amounts, rest = _amounts(folded, lang, free)
    keep(amounts)
    keep(_codes(folded))
    keep(rest)
    return tuple(sorted(taken, key=lambda t: t.start))
