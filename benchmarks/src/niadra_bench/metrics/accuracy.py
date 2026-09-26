"""Metrics 2, 4 and 5 from one pass over the dataset: every system retrieves the memory block for every
probe, the block's tokens are counted (2), the one agent answers from it and is graded (4), and privacy
cases count whether the block handed over the sensitive value (5).

The validity rule runs in the same pass: the two references (no memory, full history) answer every
case, and a case counts only when the agent gets it right with the full history and wrong without.

`context_has_answer` says whether the memory block itself holds the answer, before any agent reads it.
A value with three or more digits (a protocol, an amount, a ZIP code) counts as a whole token, as
before. A shorter number or a word cannot: the first run counted the "3" of a recurrence answer in any
date of the block, so a block that listed dates "had" every count. Those answers count only in the
pattern of their category (`_holds_short`): a count as a number of its own (never part of a date, a
time or an amount, never next to a month) on a line with a word that counts ("3 reclamações",
"Reclamações de técnico: 3", "twice"), or every occurrence listed by its reference; a deadline day
right after a word that sets one ("até o dia 15", "by the 15th"). The first run's rule is still reported, as
`context_has_answer_loose`, so runs stay comparable.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from niadra_bench.agent import Judge
from niadra_bench.dataset.model import BASE_CATEGORIES, CATEGORIES, COUNT_CATEGORIES, Case
from niadra_bench.identity import Identities
from niadra_bench.metrics import backing
from niadra_bench.stats import percentile, rate
from niadra_bench.targets.base import Target
from niadra_bench.text import contains, matches_all, matches_none, normalize, passes

Tokenizer = Callable[[str], int]

# Words that make a number next to them a count, and words that make a day next to them a deadline
# (normalized: lowercase, no accents).
_COUNTING = frozenset(
    {
        "vez", "vezes", "reclamacao", "reclamacoes", "ocorrencia", "ocorrencias", "registro", "registros",
        "contato", "contatos", "time", "times", "complaint", "complaints", "incident", "incidents",
        "report", "reports", "occurrence", "occurrences",
    }
)  # fmt: skip
_COUNT_ALONE = frozenset({"twice", "once", "thrice"})
_MONTHS = frozenset(
    {
        "janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro",
        "outubro", "novembro", "dezembro", "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december", "jan", "fev", "feb", "mar",
        "abr", "apr", "mai", "jun", "jul", "ago", "aug", "set", "sep", "sept", "out", "oct", "nov",
        "dez", "dec",
    }
)  # fmt: skip
# A word or number standing alone: not glued to a date, a time, an amount or an id by / . : , or -.
_STANDALONE = re.compile(r"(?<![\w/.:,\-])([a-z0-9]+)(?![\w]|[/.:,\-][a-z0-9])")
_DEADLINE = frozenset(
    {"ate", "dia", "prazo", "vencimento", "until", "by", "day", "deadline", "due", "before", "antes"}
)
_ORDINAL = re.compile(r"^(\d{1,2})(st|nd|rd|th)$")
_WINDOW = 3


def _digits(value: str) -> int:
    return sum(ch.isdigit() for ch in value)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def _count_stated(block: str, wanted: set[str]) -> bool:
    """A line that states the count: the count on its own, not in a date, and a word that counts."""
    for line in _fold(block).splitlines():
        words = [m.group(1) for m in _STANDALONE.finditer(line)]
        counting = any(t in _COUNTING for t in normalize(line))
        for i, word in enumerate(words):
            if word not in wanted:
                continue
            if word in _COUNT_ALONE:
                return True
            beside = {*words[max(0, i - 1) : i], *words[i + 1 : i + 3]}
            if counting and not beside & _MONTHS:
                return True
    return False


def _deadline_near(tokens: list[str], wanted: set[str]) -> bool:
    days = {w for w in wanted if w.isdigit()}
    for i, token in enumerate(tokens):
        match = _ORDINAL.match(token)
        day = match.group(1) if match else token
        if day in days and any(t in _DEADLINE for t in tokens[max(0, i - _WINDOW) : i]):
            return True
    return False


def _references(case: Case) -> list[str]:
    """The reference of every occurrence the count is about: the numbers of its key sessions."""
    refs: list[str] = []
    for session in case.sessions:
        if session.role == "key":
            refs += [t for turn in session.turns for t in normalize(turn.text) if t.isdigit() and len(t) >= 3]
    return refs


def _holds_short(block: str, tokens: list[str], group: list[str], case: Case) -> bool:
    wanted = {t for alt in group for t in normalize(alt)}
    if case.category in COUNT_CATEGORIES:
        refs = _references(case)
        return _count_stated(block, wanted) or (bool(refs) and all(contains(tokens, r) for r in refs))
    if case.category == "continuity":
        return _deadline_near(tokens, wanted)
    return any(contains(tokens, alt) for alt in group)


def context_holds_answer(block: str, case: Case) -> bool:
    """Whether a memory block holds every expected value, by the rule in this module's docstring."""
    tokens = normalize(block)
    for group in case.expect.all_of:
        specific = [alt for alt in group if _digits(alt) >= 3]
        if any(contains(tokens, alt) for alt in specific):
            continue
        short = [alt for alt in group if _digits(alt) < 3]
        if not short or not _holds_short(block, tokens, short, case):
            return False
    return True


class Answerer(Protocol):
    name: str

    async def answer(self, case: Case, memory: str) -> str: ...


@dataclass
class CaseRow:
    repetition: int
    system: str
    scenario: str | None
    case_id: str
    language: str
    category: str
    view: str | None
    purpose: str
    tokens: int
    retrieve_ms: float
    error: str | None
    context_has_answer: bool | None
    context_has_answer_loose: bool | None
    leak: bool | None
    answer: str | None
    deterministic: bool | None
    judge: bool | None
    judge_reason: str | None
    meta: dict[str, Any]
    # The answer's values (metrics/backing.py): how many it stated, and the kind of each one without a
    # source. None on rows written before 26/09/2026 and on rows that were not answered.
    backing_checked: int | None = None
    unbacked_kinds: list[str] | None = None
    # The memory block the agent received, so a verdict can be audited against what the memory held
    # (since 26/09/2026; None for the references, whose block is empty or the case's whole history).
    context: str | None = None

    def dump(self) -> dict[str, Any]:
        return asdict(self)


#: Systems whose read depends on how well the caller proved who the customer is.
VERIFIED = frozenset({"niadra"})
REFERENCES = frozenset({"no_memory", "full_history"})


def verification(system: str) -> str | None:
    """The privacy column's mechanism: "per conversation" (a level proven per conversation), "none" (the
    system hands the block to any caller, so its row reads "no mechanism", not a score), or None for the
    two references."""
    if system in REFERENCES:
        return None
    return "per conversation" if system in VERIFIED else "none"


def views_for(target: Target, case: Case, niadra_views: Sequence[str]) -> list[tuple[str | None, str]]:
    """(view, purpose): the probe channel's view is answered; other Niadra views only count tokens."""
    if target.system != "niadra":
        return [(None, "answer")]
    main = "voice" if case.probe.channel == "voice" else "chat"
    views: list[tuple[str | None, str]] = [(main, "answer")]
    return views + [(v, "tokens") for v in niadra_views if v != main]


async def _one(
    target: Target,
    case: Case,
    ids: Identities,
    view: str | None,
    purpose: str,
    repetition: int,
    agent: Answerer,
    judge: Judge | None,
    tokenizer: Tokenizer,
) -> CaseRow:
    got = await target.retrieve(case, ids, view=view)
    block = got.text
    privacy = case.category == "privacy"
    row = CaseRow(
        repetition=repetition,
        system=target.system,
        scenario=target.scenario,
        case_id=case.id,
        language=case.language,
        category=case.category,
        view=view,
        purpose=purpose,
        tokens=tokenizer(block) if block else 0,
        retrieve_ms=round(got.elapsed_ms, 2),
        error=got.error,
        context_has_answer=None if privacy else context_holds_answer(block, case),
        context_has_answer_loose=None if privacy else matches_all(block, case.expect.all_of),
        leak=(not matches_none(block, case.expect.none_of)) if privacy else None,
        answer=None,
        deterministic=None,
        judge=None,
        judge_reason=None,
        meta=got.meta,
        context=None if target.system in REFERENCES else block,
    )
    if purpose != "answer":
        return row
    row.answer = await agent.answer(case, block)
    row.backing_checked, row.unbacked_kinds = backing.check(row.answer, block, case.probe.question)
    await target.after_answer(case, ids, got.meta, row.answer, row.backing_checked, row.unbacked_kinds)
    if target.system == "full_history" and privacy:
        # The validity side of a privacy case: with the history, the agent can say the value.
        row.deterministic = matches_all(row.answer, case.expect.verified_all_of)
    else:
        row.deterministic = passes(row.answer, case.expect)
    if judge is not None and not (target.system == "full_history" and privacy):
        row.judge, row.judge_reason = await judge.grade(case, row.answer)
    elif judge is not None:
        row.judge = row.deterministic
    return row


async def run(
    targets: Sequence[Target],
    pairs: Sequence[tuple[Case, Identities]],
    *,
    repetition: int,
    agent: Answerer,
    judge: Judge | None,
    tokenizer: Tokenizer,
    niadra_views: Sequence[str],
    concurrency: int,
) -> list[CaseRow]:
    limit = asyncio.Semaphore(concurrency)

    async def guarded(target: Target, case: Case, ids: Identities, view: str | None, purpose: str) -> CaseRow:
        async with limit:
            return await _one(target, case, ids, view, purpose, repetition, agent, judge, tokenizer)

    jobs = [
        guarded(target, case, ids, view, purpose)
        for target in targets
        for case, ids in pairs
        for view, purpose in views_for(target, case, niadra_views)
    ]
    return list(await asyncio.gather(*jobs))


def _verdict(row: CaseRow) -> bool | None:
    return row.judge if row.judge is not None else row.deterministic


def valid_cases(rows: Sequence[CaseRow], cases: dict[str, Case]) -> tuple[set[str], list[str]]:
    """Cases the agent answers right with the full history and wrong with no memory."""
    full = {r.case_id: _verdict(r) for r in rows if r.system == "full_history" and r.purpose == "answer"}
    none = {r.case_id: r for r in rows if r.system == "no_memory" and r.purpose == "answer"}
    valid: set[str] = set()
    excluded: list[str] = []
    for case_id, with_history in full.items():
        without = none.get(case_id)
        if without is None:
            continue
        if without.category == "privacy":
            # The verified side of a privacy case: without history the agent cannot produce the value.
            failed_without = not matches_all(without.answer or "", cases[case_id].expect.verified_all_of)
        else:
            failed_without = not bool(_verdict(without))
        if with_history and failed_without:
            valid.add(case_id)
        else:
            excluded.append(case_id)
    return valid, sorted(excluded)


def summarize(rows: Sequence[CaseRow], valid: set[str]) -> list[dict[str, Any]]:
    """One line per system and scenario: accuracy over valid cases, per category, tokens and leaks."""
    groups: dict[tuple[str, str | None], list[CaseRow]] = {}
    for row in rows:
        groups.setdefault((row.system, row.scenario), []).append(row)
    out: list[dict[str, Any]] = []
    for (system, scenario), group in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        answered = [r for r in group if r.purpose == "answer" and r.case_id in valid]
        by_category: dict[str, Any] = {}
        # The v1 categories always; a later version's only when its cases were asked.
        asked = {r.category for r in group}
        for category in (c for c in CATEGORIES if c in BASE_CATEGORIES or c in asked):
            subset = [r for r in answered if r.category == category]
            by_category[category] = {
                "cases": len(subset),
                "deterministic": rate(sum(bool(r.deterministic) for r in subset), len(subset)),
                "judge": rate(sum(bool(r.judge) for r in subset if r.judge is not None), len(subset))
                if any(r.judge is not None for r in subset)
                else None,
            }
        tokens: dict[str, Any] = {}
        for view in sorted({r.view or "default" for r in group}):
            counts = [r.tokens for r in group if (r.view or "default") == view]
            tokens[view] = {
                "median": statistics.median(counts) if counts else None,
                "p95": percentile(counts, 95),
                "mean": round(statistics.fmean(counts), 1) if counts else None,
            }
        privacy = [r for r in group if r.purpose == "answer" and r.category == "privacy"]
        judged = [r for r in answered if r.judge is not None]
        context_known = [r for r in answered if r.context_has_answer is not None]
        out.append(
            {
                "system": system,
                "scenario": scenario,
                "cases": len(answered),
                "deterministic": rate(sum(bool(r.deterministic) for r in answered), len(answered)),
                "judge": rate(sum(bool(r.judge) for r in judged), len(judged)) if judged else None,
                "context_has_answer": rate(
                    sum(bool(r.context_has_answer) for r in context_known), len(context_known)
                ),
                "context_has_answer_loose": rate(
                    sum(bool(r.context_has_answer_loose) for r in context_known), len(context_known)
                ),
                "by_category": by_category,
                "tokens": tokens,
                "privacy": {
                    "cases": len(privacy),
                    "leaks": sum(bool(r.leak) for r in privacy),
                    "leak_rate": rate(sum(bool(r.leak) for r in privacy), len(privacy)),
                    # Whether the system reads by a level the caller proved (the harness's runs since
                    # 26/09/2026; earlier results do not carry it).
                    "verification": verification(system),
                },
                "retrieve_errors": sum(1 for r in group if r.error),
                "retrieve_ms_p50": percentile([r.retrieve_ms for r in group if not r.error], 50),
            }
        )
    return out
