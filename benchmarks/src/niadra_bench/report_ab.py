"""The delta of an A/B (`bench ab`): one cell per figure, both sides' median and range over the
repetitions, and the candidate's median minus the baseline's; the cases that changed verdict; the
determinism verdict of `--same`; and the Markdown tables of `ab.md`.

Every figure comes from each side's summary as the runner aggregates it (`runner.aggregate`, the format of
`summary.json`), so an A/B reads its numbers exactly as a published run does. `context_has_answer` per
category is the one addition, computed here from the case rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from niadra_bench import stats
from niadra_bench.dataset.model import CATEGORIES
from niadra_bench.metrics.accuracy import CaseRow

#: Sections whose figures must come out identical when a side is run against itself.
EXACT_SECTIONS = frozenset({"accuracy", "context_by_category", "tokens", "privacy"})
#: What each environment kind's numbers are good for.
NOTES = {
    "local-cell": (
        "Local cell: niadra-back's in-memory flow harness with a rule extractor and hash embeddings. "
        "For comparing two settings of the same code; never published, and no absolute number here "
        "says what the region would measure. With the context agent the answer is the memory block "
        "itself, so its exact check matches loosely (any 2 in a date counts for a count of 2): for "
        "recurrence and deadlines read context_has_answer."
    ),
    "dry-run": "niadra-mock in-process: the harness's plumbing only.",
    "local": "Outside the region: never published.",
    "region": "Measured inside the cloud region; the site imports no A/B, only a run's summary.json.",
}
_CASE_FIELDS = ("tokens", "context_has_answer", "context_has_answer_loose", "leak", "deterministic", "judge")


def _verdict(row: CaseRow) -> bool | None:
    return row.judge if row.judge is not None else row.deterministic


def context_by_category(rows: Sequence[CaseRow], valid: set[str]) -> dict[str, float | None]:
    """Share of valid answer rows whose memory block held the answer, per category (Niadra only)."""
    out: dict[str, float | None] = {}
    answered = [r for r in rows if r.system == "niadra" and r.purpose == "answer" and r.case_id in valid]
    for category in CATEGORIES:
        subset = [r for r in answered if r.category == category and r.context_has_answer is not None]
        if subset:
            out[category] = stats.rate(sum(bool(r.context_has_answer) for r in subset), len(subset))
    return out


def aggregate_context(reps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    categories = [c for c in CATEGORIES if any(c in (r.get("context_by_category") or {}) for r in reps)]
    return {
        category: stats.across([(r.get("context_by_category") or {}).get(category) for r in reps], 4)
        for category in categories
    }


def _niadra(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [r for r in rows if str(r.get("system", "")).startswith("niadra")]


def _results(metrics: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    return list((metrics.get(name) or {}).get("results") or [])


def _cell(section: str, line: str, field: str, unit: str, base: Any, cand: Any) -> dict[str, Any] | None:
    base = base if isinstance(base, dict) else stats.across([])
    cand = cand if isinstance(cand, dict) else stats.across([])
    if base.get("median") is None and cand.get("median") is None:
        return None
    delta = None
    if base.get("median") is not None and cand.get("median") is not None:
        delta = round(cand["median"] - base["median"], 4)
    return {
        "section": section,
        "line": line,
        "field": field,
        "unit": unit,
        "exact": section in EXACT_SECTIONS,
        "baseline": base,
        "candidate": cand,
        "delta": delta,
    }


def _by_key(
    rows: Iterable[Mapping[str, Any]], key: Iterable[str]
) -> dict[tuple[Any, ...], Mapping[str, Any]]:
    names = tuple(key)
    return {tuple(r.get(k) for k in names): r for r in rows}


def cells(base: Mapping[str, Any], cand: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One cell per figure both sides can have, in the order of the tables."""
    out: list[dict[str, Any] | None] = []

    b_acc = _by_key(_niadra(_results(base, "accuracy")), ("system", "scenario"))
    c_acc = _by_key(_niadra(_results(cand, "accuracy")), ("system", "scenario"))
    for key in sorted(set(b_acc) | set(c_acc), key=str):
        b, c = b_acc.get(key, {}), c_acc.get(key, {})
        for field in ("judge", "deterministic", "context_has_answer"):
            out.append(_cell("accuracy", "all categories", field, "share", b.get(field), c.get(field)))
        b_cat, c_cat = b.get("by_category") or {}, c.get("by_category") or {}
        for category in [x for x in CATEGORIES if x in b_cat or x in c_cat]:
            for field in ("judge", "deterministic"):
                out.append(
                    _cell(
                        "accuracy",
                        category,
                        field,
                        "share",
                        (b_cat.get(category) or {}).get(field),
                        (c_cat.get(category) or {}).get(field),
                    )
                )
    b_ctx, c_ctx = base.get("context_by_category") or {}, cand.get("context_by_category") or {}
    for category in [x for x in CATEGORIES if x in b_ctx or x in c_ctx]:
        out.append(
            _cell("context_by_category", category, "context_has_answer", "share", b_ctx.get(category),
                  c_ctx.get(category))
        )  # fmt: skip

    b_tok = _by_key(_niadra(_results(base, "tokens")), ("view",))
    c_tok = _by_key(_niadra(_results(cand, "tokens")), ("view",))
    for key in sorted(set(b_tok) | set(c_tok), key=str):
        for field in ("median", "p95"):
            out.append(
                _cell("tokens", str(key[0]), field, "tokens", b_tok.get(key, {}).get(field),
                      c_tok.get(key, {}).get(field))
            )  # fmt: skip

    b_priv = _by_key(_niadra(_results(base, "privacy")), ("system",))
    c_priv = _by_key(_niadra(_results(cand, "privacy")), ("system",))
    for key in sorted(set(b_priv) | set(c_priv), key=str):
        for field in ("leaks", "leak_rate"):
            out.append(
                _cell("privacy", "V0 reads", field, "count" if field == "leaks" else "share",
                      b_priv.get(key, {}).get(field), c_priv.get(key, {}).get(field))
            )  # fmt: skip

    b_lat = _by_key(_niadra(_results(base, "latency")), ("path", "rate"))
    c_lat = _by_key(_niadra(_results(cand, "latency")), ("path", "rate"))
    for key in sorted(set(b_lat) | set(c_lat), key=str):
        for field in ("p50", "p95"):
            out.append(
                _cell("latency", f"context {key[0]} {key[1]}/s", field, "ms",
                      b_lat.get(key, {}).get(field), c_lat.get(key, {}).get(field))
            )  # fmt: skip
    for section in ("history", "ingest"):
        b_ops = _by_key(_niadra(_results(base, section)), ("operation", "path", "rate"))
        c_ops = _by_key(_niadra(_results(cand, section)), ("operation", "path", "rate"))
        for key in sorted(set(b_ops) | set(c_ops), key=str):
            for field in ("p50", "p95"):
                out.append(
                    _cell(section, f"{key[0]} {key[1]} {key[2]}/s", field, "ms",
                          b_ops.get(key, {}).get(field), c_ops.get(key, {}).get(field))
                )  # fmt: skip

    b_cost = _by_key(_niadra(_results(base, "cost")), ("variant",))
    c_cost = _by_key(_niadra(_results(cand, "cost")), ("variant",))
    for key in sorted(set(b_cost) | set(c_cost), key=str):
        for field in ("memory_usd_per_1000", "agent_prompt_usd_per_1000"):
            out.append(
                _cell("cost", str(key[0]), field, "USD per 1000 conversations",
                      b_cost.get(key, {}).get(field), c_cost.get(key, {}).get(field))
            )  # fmt: skip
    return [c for c in out if c is not None]


def _answers(rows: Iterable[CaseRow]) -> dict[str, CaseRow]:
    return {r.case_id: r for r in rows if r.system == "niadra" and r.purpose == "answer"}


def flips(
    base: Mapping[int, Sequence[CaseRow]],
    cand: Mapping[int, Sequence[CaseRow]],
    valid: Mapping[int, set[str]],
) -> dict[str, Any]:
    """Valid cases whose answer verdict, or whose memory block's answer, changed side, per category."""
    out: dict[str, dict[str, dict[str, list[str]]]] = {"answer": {}, "context": {}}
    for n in sorted(set(base) & set(cand)):
        b_rows, c_rows = _answers(base[n]), _answers(cand[n])
        for case_id in sorted(set(b_rows) & set(c_rows) & valid.get(n, set())):
            b, c = b_rows[case_id], c_rows[case_id]
            for kind, before, after in (
                ("answer", _verdict(b), _verdict(c)),
                ("context", b.context_has_answer, c.context_has_answer),
            ):
                if before is None or after is None or bool(before) == bool(after):
                    continue
                slot = out[kind].setdefault(b.category, {"gained": [], "lost": []})
                slot["gained" if after else "lost"].append(f"{case_id}@{n}")
    return out


def determinism(
    table: Sequence[Mapping[str, Any]],
    base: Mapping[int, Sequence[CaseRow]],
    cand: Mapping[int, Sequence[CaseRow]],
) -> dict[str, Any]:
    """`--same`: every exact figure identical in every repetition, and every Niadra case row too."""
    differences: list[str] = []
    for cell in table:
        if cell["exact"] and cell["baseline"].get("runs") != cell["candidate"].get("runs"):
            differences.append(
                f"{cell['section']} {cell['line']} {cell['field']}: "
                f"{cell['baseline'].get('runs')} != {cell['candidate'].get('runs')}"
            )
    for n in sorted(set(base) | set(cand)):
        b_rows = {(r.case_id, r.view, r.purpose): r for r in base.get(n, []) if r.system == "niadra"}
        c_rows = {(r.case_id, r.view, r.purpose): r for r in cand.get(n, []) if r.system == "niadra"}
        for key in sorted(set(b_rows) | set(c_rows), key=str):
            b, c = b_rows.get(key), c_rows.get(key)
            if b is None or c is None:
                differences.append(f"repetition {n} {key[0]} {key[1]}: only one side read it")
                continue
            for name in _CASE_FIELDS:
                if getattr(b, name) != getattr(c, name):
                    differences.append(
                        f"repetition {n} {key[0]} {key[1]} {name}: {getattr(b, name)} != {getattr(c, name)}"
                    )
    return {
        "passed": not differences,
        "rule": "accuracy, context_has_answer, privacy and token figures identical per repetition and per "
        "case; latency free",
        "differences": differences[:200],
        "difference_count": len(differences),
    }


def _fmt(value: Any, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "share":
        return f"{value * 100:.1f}%"
    if unit.startswith("USD"):
        return f"{value:.4f}"
    return f"{value:g}"


def _side(stat: Mapping[str, Any], unit: str) -> str:
    median = stat.get("median")
    if median is None:
        return "n/a"
    text = _fmt(median, unit)
    runs = [v for v in stat.get("runs") or [] if v is not None]
    if len(runs) > 1:
        text += f" ({_fmt(stat.get('min'), unit)} to {_fmt(stat.get('max'), unit)})"
    return text


def _delta(cell: Mapping[str, Any]) -> str:
    delta = cell.get("delta")
    if delta is None:
        return "n/a"
    if cell["unit"] == "share":
        return f"{delta * 100:+.1f} pp"
    if cell["unit"].startswith("USD"):
        return f"{delta:+.4f}"
    return f"{delta:+g}"


_TITLES = {
    "accuracy": "Accuracy (valid cases; the judge when there is one, else the exact check)",
    "context_by_category": "context_has_answer by category",
    "tokens": "Tokens per turn",
    "privacy": "Privacy",
    "latency": "Context latency (metric 1)",
    "history": "History navigation (metric 8)",
    "ingest": "Ingestion acknowledgement (metric 9)",
    "cost": "Cost",
}


def markdown(document: Mapping[str, Any]) -> str:
    env = document["environment"]
    lines = [
        f"# A/B {document['ab_id']}" + (f": {document['label']}" if document.get("label") else ""),
        "",
        f"- Where: `{env['kind']}`. {env['note']}",
        f"- Mode: `{document['mode']}`; dataset {document['dataset']['version']} "
        f"({document['dataset']['cases']} cases, valid per repetition {document['dataset']['valid']}); "
        f"{document['repetitions']} repetition(s); agent `{document['agent']}`.",
        f"- Baseline: `{_env(document['baseline']['env'])}`; "
        f"candidate: `{_env(document['candidate']['env'])}`{_config_file(document)}",
        "- Each side: median over the repetitions, and the range when there is more than one. Delta: "
        "candidate median minus baseline median.",
    ]
    if env.get("local_cell"):
        cell = env["local_cell"]
        base, cand = document["baseline"], document["candidate"]
        where = f"`{base.get('commit')}` at `{base.get('checkout')}`"
        if (cand.get("checkout"), cand.get("commit")) != (base.get("checkout"), base.get("commit")):
            where = f"baseline {where}, candidate `{cand.get('commit')}` at `{cand.get('checkout')}`"
        lines.append(f"- niadra-back {where}; clock {cell['now']}.")
    if document.get("incomplete"):
        lines.append(f"- Incomplete: {document['incomplete']}")
    if (verdict := document.get("determinism")) is not None:
        lines.append(
            f"- Determinism: {'passed' if verdict['passed'] else 'FAILED'} "
            f"({verdict['difference_count']} differences)"
        )
    table = document["delta"]
    for section, title in _TITLES.items():
        rows = [c for c in table if c["section"] == section]
        if not rows:
            continue
        lines += ["", f"## {title}", "", "| Line | Figure | Baseline | Candidate | Delta |"]
        lines.append("|---|---|---|---|---|")
        for c in rows:
            lines.append(
                f"| {c['line']} | {c['field']} | {_side(c['baseline'], c['unit'])} | "
                f"{_side(c['candidate'], c['unit'])} | {_delta(c)} |"
            )
    flipped = document.get("flips") or {}
    if any(flipped.get(k) for k in ("answer", "context")):
        lines += ["", "## Cases that changed", "", "| Category | What | Gained | Lost |", "|---|---|---|---|"]
        for kind in ("answer", "context"):
            for category, slot in sorted((flipped.get(kind) or {}).items()):
                lines.append(f"| {category} | {kind} | {len(slot['gained'])} | {len(slot['lost'])} |")
    verdict = document.get("determinism")
    if verdict and verdict["differences"]:
        lines += ["", "## Differences", ""] + [f"- {d}" for d in verdict["differences"][:50]]
    return "\n".join(lines) + "\n"


def _env(env: Mapping[str, str]) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(env.items())) or "the server's own settings"


def _config_file(document: Mapping[str, Any]) -> str:
    name = document["candidate"].get("config_file")
    return f" with `{name}`" if name else ""
