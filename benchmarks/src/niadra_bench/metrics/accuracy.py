"""Metrics 2, 4 and 5 from one pass over the dataset: every system retrieves the memory block for every
probe, the block's tokens are counted (2), the one agent answers from it and is graded (4), and privacy
cases count whether the block handed over the sensitive value (5).

The validity rule runs in the same pass: the two references (no memory, full history) answer every
case, and a case counts only when the agent gets it right with the full history and wrong without.
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from niadra_bench.agent import Judge
from niadra_bench.dataset.model import CATEGORIES, Case
from niadra_bench.identity import Identities
from niadra_bench.stats import percentile, rate
from niadra_bench.targets.base import Target
from niadra_bench.text import matches_all, matches_none, passes

Tokenizer = Callable[[str], int]


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
    leak: bool | None
    answer: str | None
    deterministic: bool | None
    judge: bool | None
    judge_reason: str | None
    meta: dict[str, Any]

    def dump(self) -> dict[str, Any]:
        return asdict(self)


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
        context_has_answer=None if privacy else matches_all(block, case.expect.all_of),
        leak=(not matches_none(block, case.expect.none_of)) if privacy else None,
        answer=None,
        deterministic=None,
        judge=None,
        judge_reason=None,
        meta=got.meta,
    )
    if purpose != "answer":
        return row
    row.answer = await agent.answer(case, block)
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
        for category in CATEGORIES:
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
                "by_category": by_category,
                "tokens": tokens,
                "privacy": {
                    "cases": len(privacy),
                    "leaks": sum(bool(r.leak) for r in privacy),
                    "leak_rate": rate(sum(bool(r.leak) for r in privacy), len(privacy)),
                },
                "retrieve_errors": sum(1 for r in group if r.error),
                "retrieve_ms_p50": percentile([r.retrieve_ms for r in group if not r.error], 50),
            }
        )
    return out
