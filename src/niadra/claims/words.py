"""Numbers written in words, in Portuguese, English and Spanish, from zero to the millions: "quinze", "dois
mil e quinhentos", "twenty-five", "doscientos treinta". The parser reads them only right before a unit, a
currency or a percent word ("quinze dias úteis", "trezentos reais"): a number word alone is never a number,
because "um", "one" and "un" are also articles."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from niadra.claims.text import Word

_UNITS = {
    "pt": {
        "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6,
        "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12, "treze": 13, "quatorze": 14,
        "catorze": 14, "quinze": 15, "dezesseis": 16, "dezasseis": 16, "dezessete": 17, "dezassete": 17,
        "dezoito": 18, "dezenove": 19, "dezanove": 19, "vinte": 20, "trinta": 30, "quarenta": 40,
        "cinquenta": 50, "cincoenta": 50, "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90,
        "cem": 100, "cento": 100, "duzentos": 200, "duzentas": 200, "trezentos": 300, "trezentas": 300,
        "quatrocentos": 400, "quatrocentas": 400, "quinhentos": 500, "quinhentas": 500, "seiscentos": 600,
        "seiscentas": 600, "setecentos": 700, "setecentas": 700, "oitocentos": 800, "oitocentas": 800,
        "novecentos": 900, "novecentas": 900,
    },
    "en": {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
        "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    },
    "es": {
        "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
        "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
        "quince": 15, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20,
        "veintiun": 21, "veintiuno": 21, "veintiuna": 21, "veintidos": 22, "veintitres": 23,
        "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28,
        "veintinueve": 29, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70,
        "ochenta": 80, "noventa": 90, "cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200,
        "trescientos": 300, "trescientas": 300, "cuatrocientos": 400, "cuatrocientas": 400,
        "quinientos": 500, "quinientas": 500, "seiscientos": 600, "seiscientas": 600, "setecientos": 700,
        "setecientas": 700, "ochocientos": 800, "ochocientas": 800, "novecientos": 900,
        "novecientas": 900,
    },
}  # fmt: skip
_HUNDRED = {"en": frozenset({"hundred"})}
_THOUSAND = {"pt": frozenset({"mil"}), "en": frozenset({"thousand"}), "es": frozenset({"mil"})}
_MILLION = {
    "pt": frozenset({"milhao", "milhoes"}),
    "en": frozenset({"million", "millions"}),
    "es": frozenset({"millon", "millones"}),
}
_AND = {"pt": "e", "en": "and", "es": "y"}


def _is_number_word(lang: str, word: str) -> bool:
    return (
        word in _UNITS[lang]
        or word in _HUNDRED.get(lang, ())
        or word in _THOUSAND[lang]
        or word in _MILLION[lang]
    )


def read_words(lang: str, found: Sequence[Word], i: int) -> tuple[Decimal, int] | None:
    """The number written in words from `found[i]` on, and the index past its last word; None when no
    number starts there. "e", "and" and "y" join number words and never end or start one; a hyphen separates
    words, so "twenty-five" reads as "twenty five"."""
    total = current = 0
    j = i
    # After a unit or a teen, another one needs "e", "and" or "y" first: "un veinticinco" is two numbers.
    small = False
    while j < len(found):
        word = found[j].text
        if word == _AND[lang] and j > i and j + 1 < len(found) and _is_number_word(lang, found[j + 1].text):
            j += 1
            small = False
            continue
        if small and word in _UNITS[lang]:
            break
        if word in _MILLION[lang]:
            total = (total + max(current, 1)) * 1_000_000
            current = 0
        elif word in _THOUSAND[lang]:
            total += max(current, 1) * 1000
            current = 0
        elif word in _HUNDRED.get(lang, ()):
            current = max(current, 1) * 100
        elif word in _UNITS[lang]:
            current += _UNITS[lang][word]
        else:
            break
        small = word in _UNITS[lang] and _UNITS[lang][word] < 20
        j += 1
    while j > i and found[j - 1].text == _AND[lang]:
        j -= 1
    return (Decimal(total + current), j) if j > i else None
