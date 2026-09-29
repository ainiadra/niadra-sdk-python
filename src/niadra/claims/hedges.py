"""Hedges (the claim contract spec, section 5.4): a number the output says it cannot confirm, says as a value
that no longer holds, or doubts, is not asserted, and no category detects it. "Não consigo confirmar se o
frete de R$ 24,90 ainda vale" states no price.

A number's clause is its sentence cut at clause breaks: a comma, semicolon or colon before a space, a
parenthesis, a dash, and the clause words ("mas", "but", "porque"). A break inside a number does not count. A
number is hedged when:

1. a denial stands before it in its clause ("não consigo confirmar", "I can't confirm", "whether");
2. it is the number of its clause nearest to a past marker ("o valor anterior", "previously listed"), on
   either side, within `WINDOW` words, with no word of the present between them ("now", "agora");
3. it is the number of its clause nearest before a doubt ("pode ter mudado", "has changed"), within `WINDOW`
   words;
4. its clause follows one that is only an opener ("Antes, ele estava em R$ 1.240,00");
5. its clause holds a past word ("foi", "was", "on file"), and a later clause of its sentence holds a denial
   followed by a word of continuity ("mas não consigo confirmar se esse total continua igual");
6. a negation stands right before it ("$689.00 per month, not $612.00").

A hedge that governs something else leaves the number asserted: "O total é R$ 500, mas não consigo confirmar o
prazo" states R$ 500. The words match whole words of the folded text, in sequence, with both apostrophes
alike, and never inside a number.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from niadra.claims.numbers import Mention
from niadra.claims.roles import WINDOW
from niadra.claims.text import as_words, sentence_of, words

DENIALS = (
    "nao posso confirmar", "nao consigo confirmar", "nao tenho como confirmar", "nao da para confirmar",
    "nao e possivel confirmar", "nao posso garantir", "nao consigo garantir", "nao tenho como garantir",
    "nao posso afirmar", "nao consigo afirmar", "nao tenho como afirmar", "nao posso verificar",
    "nao consigo verificar", "nao tenho como verificar", "nao tenho confirmacao", "sem confirmacao",
    "nao sei se", "nao tenho informacao", "nao tenho informacoes", "nao tenho a informacao",
    "nao tenho essa informacao", "preciso verificar", "precisa verificar", "precisamos verificar",
    "confirmar se", "verificar se", "conferir se", "saber se", "dizer se",
    "can't confirm", "cannot confirm", "can not confirm", "couldn't confirm", "unable to confirm",
    "not able to confirm", "can't verify", "cannot verify", "unable to verify", "can't guarantee",
    "cannot guarantee", "can't promise", "cannot promise", "don't have confirmation",
    "do not have confirmation", "no confirmation", "don't have information", "do not have information",
    "don't have the information", "do not have the information", "need to verify", "whether", "confirm if",
    "verify if", "check if", "know if", "sure if",
    "no puedo confirmar", "no logro confirmar", "no consigo confirmar", "no tengo como confirmar",
    "no es posible confirmar", "no puedo garantizar", "no puedo asegurar", "no puedo verificar",
    "no tengo confirmacion", "sin confirmacion", "no se si", "no tengo informacion", "necesito verificar",
    "hay que verificar", "confirmar si", "verificar si", "saber si", "decir si",
)  # fmt: skip
"""Before a number, to the end of its clause: the output says it cannot confirm what follows, or asks it."""

PAST_MARKERS = (
    "valor anterior", "preco anterior", "total anterior", "valor antigo", "preco antigo", "o anterior",
    "a anterior", "os anteriores", "anteriormente", "quando visto", "quando vista", "quando voce viu",
    "antes era", "era antes",
    "previous price", "previous total", "previous value", "earlier price", "earlier total", "earlier value",
    "old price", "previous amount", "earlier amount", "old amount", "was the earlier", "was previously",
    "were previously", "previously listed", "previously quoted", "previously shown", "previously priced",
    "seen previously", "shown previously", "quoted previously", "listed previously", "last shown",
    "last quoted", "last listed", "when seen", "when you saw it",
    "precio anterior", "precio antiguo", "el anterior", "la anterior", "cuando lo vio", "cuando lo viste",
)  # fmt: skip
"""Beside a number: a value that no longer holds, as the one seen before."""

DOUBTS = (
    "pode ter mudado", "pode ter sido alterado", "pode ter sido alterada", "pode ter sido atualizado",
    "pode ter sido atualizada", "pode nao valer", "pode nao estar valendo", "nao vale mais", "ja nao vale",
    "precisa ser confirmado", "precisa ser confirmada", "precisa ser verificado", "precisa ser verificada",
    "nao esta confirmado", "nao esta confirmada", "nao foi confirmado", "nao foi confirmada", "mudou",
    "foi alterado", "foi alterada", "venceu", "expirou", "esta desatualizado", "esta desatualizada",
    "may have changed", "might have changed", "could have changed", "has changed", "have changed",
    "had changed", "is outdated", "is out of date", "no longer applies", "no longer holds", "no longer valid",
    "may no longer", "might no longer", "may not apply", "has expired", "expired", "needs to be confirmed",
    "needs to be verified", "needs to be checked", "is not confirmed", "isn't confirmed", "not confirmed",
    "unconfirmed",
    "puede haber cambiado", "pudo haber cambiado", "ha cambiado", "ya cambio", "ya no vale",
    "ya no es valido", "ya no aplica", "vencio", "expiro", "necesita confirmarse", "no esta confirmado",
    "no esta confirmada", "esta desactualizado", "esta desactualizada",
)  # fmt: skip
"""After a number: the output doubts that it still holds."""

OPENERS = (
    "antes", "anteriormente", "da ultima vez", "na ultima vez", "na ultima consulta", "na ultima cotacao",
    "previously", "earlier", "before", "last time", "the last time", "in the last quote",
    "la ultima vez", "en la ultima cotizacion",
)  # fmt: skip
"""A clause of its own before the number's: what follows is said as it was."""

PAST_WORDS = (
    "foi", "foram", "era", "eram", "estava", "estavam", "ficou", "ficava", "custava", "custavam",
    "registrado", "registrada", "informado", "informada",
    "was", "were", "had been", "used to be", "on file", "on record", "quoted",
    "fue", "fueron", "eran", "estaba", "estaban", "costaba", "costaban", "quedo",
)  # fmt: skip
"""In the number's clause, with a later denial that it still holds: the number said as it was."""

CONTINUITY = (
    "ainda", "continua", "continuam", "segue", "seguem", "mesmo", "mesma", "igual", "atual", "atualizado",
    "atualizada", "vigente", "vale", "valendo", "valido", "valida", "mantem", "mantido", "mantida",
    "still", "remains", "remain", "current", "same", "valid", "applies", "apply", "holds", "unchanged",
    "anymore",
    "todavia", "aun", "sigue", "siguen", "mismo", "misma", "actual", "mantiene",
)  # fmt: skip
"""After a denial: what it denies is that the value still holds."""

NEGATIONS = (
    "nao", "em vez de", "ao inves de",
    "not", "instead of", "rather than",
    "en vez de", "en lugar de",
)  # fmt: skip
"""Right before a number: the output says it is not that. Not the Spanish "no": it is also the Portuguese "no"
("no dia 20")."""

PRESENT = (
    "agora", "hoje", "atual", "atualmente", "novo", "nova", "para",
    "now", "today", "current", "currently", "new", "to",
    "ahora", "hoy", "actual", "actualmente", "nuevo", "nueva",
)  # fmt: skip
"""Between a past marker and a number: the number is the one that holds now."""

CLAUSE_WORDS = (
    "mas", "porem", "contudo", "entretanto", "pois", "porque", "ja que", "e",
    "but", "however", "because", "since", "although", "though", "and",
    "pero", "sino", "pues", "aunque", "ya que", "y",
)  # fmt: skip
"""Words that start a clause. The Portuguese "e" only as written: folded, "é" is "e" too."""

_BREAK = re.compile(r"[,;:](?=\s|$)|[()\u2014\u2013]|(?<=\s)-(?=\s)")


@dataclass(frozen=True, slots=True)
class _Hit:
    first: int
    past: int
    """Word indexes: the phrase's first word, and the one after its last."""
    start: int
    end: int


def _plain(word: str) -> str:
    return word.replace("\u2019", "'")


def _phrases(entries: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(_plain(w) for w in as_words(e)) for e in entries)


_LISTS = {
    "denials": _phrases(DENIALS),
    "past": _phrases(PAST_MARKERS),
    "doubts": _phrases(DOUBTS),
    "openers": _phrases(OPENERS),
    "past_words": _phrases(PAST_WORDS),
    "continuity": _phrases(CONTINUITY),
    "negations": _phrases(NEGATIONS),
    "present": _phrases(PRESENT),
    "clause": _phrases(CLAUSE_WORDS),
}


def hedged(text: str, numbers: Sequence[Mention]) -> frozenset[Mention]:
    """The numbers of `numbers` (the output's mentions that are not labels) the output does not assert."""
    if not numbers:
        return frozenset()
    found = words(text)
    tokens = [_plain(w.text) for w in found]
    spans = [(m.start, m.end) for m in numbers]

    def inside(start: int, end: int) -> bool:
        return any(s < end and start < e for s, e in spans)

    def hits(name: str) -> list[_Hit]:
        at = set()
        for phrase in _LISTS[name]:
            for i in range(len(tokens) - len(phrase) + 1):
                if tuple(tokens[i : i + len(phrase)]) == phrase:
                    start, end = found[i].start, found[i + len(phrase) - 1].end
                    if not inside(start, end):
                        at.add(_Hit(i, i + len(phrase), start, end))
        return sorted(at, key=lambda h: (h.first, h.past))

    breaks = [(m.start(), m.end()) for m in _BREAK.finditer(text) if not inside(m.start(), m.end())]
    breaks += [(h.start, h.end) for h in hits("clause") if tokens[h.first] != "e" or text[h.start] in "eE"]

    def clause(start: int, end: int) -> tuple[int, int]:
        low, high = sentence_of(text, start)
        low = max([low, *(e for s, e in breaks if e <= start)])
        return low, min([high, *(s for s, e in breaks if s >= end)])

    def within(hit: _Hit, span: tuple[int, int]) -> bool:
        return span[0] <= hit.start and hit.end <= span[1]

    indexes: list[tuple[int, int] | None] = []
    for m in numbers:
        mine = [i for i, w in enumerate(found) if m.start <= w.start < m.end]
        indexes.append((mine[0], mine[-1] + 1) if mine else None)
    clauses = [clause(m.start, m.end) for m in numbers]
    denials, present = hits("denials"), {h.first for h in hits("present")}
    out: set[Mention] = set()

    # 1. A denial before the number, in its clause.
    for m, (low, _) in zip(numbers, clauses, strict=True):
        if any(within(d, (low, m.start)) for d in denials):
            out.add(m)

    def nearest(hit: _Hit, *, either_side: bool) -> Mention | None:
        """The number of the hit's clause nearest to it within `WINDOW` words: before it, or on either side
        with no word of the present between them. At the same distance, the one after it."""
        best: tuple[int, int, Mention] | None = None
        for m, span, own in zip(numbers, indexes, clauses, strict=True):
            if span is None or not within(hit, own):
                continue
            if span[1] <= hit.first:
                gap, side, between = hit.first - span[1], 1, range(span[1], hit.first)
            elif span[0] >= hit.past and either_side:
                gap, side, between = span[0] - hit.past, 0, range(hit.past, span[0])
            else:
                continue
            if gap > WINDOW or (either_side and present.intersection(between)):
                continue
            if best is None or (gap, side) < best[:2]:
                best = (gap, side, m)
        return best[2] if best is not None else None

    # 2. The number nearest to a past marker; 3. the number nearest before a doubt.
    for hit in hits("past"):
        if (near := nearest(hit, either_side=True)) is not None:
            out.add(near)
    for hit in hits("doubts"):
        if (near := nearest(hit, either_side=False)) is not None:
            out.add(near)

    # 4. The clause after one that is only an opener.
    for hit in hits("openers"):
        low, high = clause(hit.start, hit.end)
        following = [e for s, e in breaks if s == high]
        if text[low : hit.start].strip() or text[hit.end : high].strip() or not following:
            continue
        out.update(m for m, own in zip(numbers, clauses, strict=True) if own[0] == max(following))

    # 5. A past word in the number's clause, and a later clause of its sentence that denies it still holds.
    past_words, continuity = hits("past_words"), hits("continuity")
    for m, own in zip(numbers, clauses, strict=True):
        if m in out or not any(within(v, own) for v in past_words):
            continue
        end = sentence_of(text, m.start)[1]
        for d in denials:
            if own[1] <= d.start and d.end <= end:
                limit = (d.end, clause(d.start, d.end)[1])
                if any(within(c, limit) for c in continuity):
                    out.add(m)
                    break

    # 6. A negation right before the number.
    for hit in hits("negations"):
        out.update(m for m, span in zip(numbers, indexes, strict=True) if span and span[0] == hit.past)
    return frozenset(out)
