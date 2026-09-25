from niadra_bench import config as bench_config
from niadra_bench.agent import JUDGE_PROMPT, judge_prompt
from niadra_bench.dataset import generate
from niadra_bench.metrics import cost
from niadra_bench.metrics.accuracy import CaseRow, context_holds_answer, summarize, valid_cases
from niadra_bench.text import matches_all


def _row(
    system: str, case_id: str, category: str, *, ok: bool, answer: str = "x", tokens: int = 10
) -> CaseRow:
    return CaseRow(
        repetition=1,
        system=system,
        scenario=None,
        case_id=case_id,
        language="en",
        category=category,
        view=None,
        purpose="answer",
        tokens=tokens,
        retrieve_ms=1.0,
        error=None,
        context_has_answer=ok,
        context_has_answer_loose=ok,
        leak=None,
        answer=answer,
        deterministic=ok,
        judge=None,
        judge_reason=None,
        meta={},
    )


def test_validity_keeps_cases_right_with_history_and_wrong_without(cases) -> None:
    by_id = {c.id: c for c in cases}
    a, b, c = (x.id for x in cases[:3])
    rows = [
        _row("full_history", a, "continuity", ok=True),
        _row("no_memory", a, "continuity", ok=False),
        _row("full_history", b, "continuity", ok=False),
        _row("no_memory", b, "continuity", ok=False),
        _row("full_history", c, "continuity", ok=True),
        _row("no_memory", c, "continuity", ok=True),
    ]
    valid, excluded = valid_cases(rows, by_id)
    assert valid == {a} and excluded == sorted([b, c])


def test_summary_scores_only_valid_cases() -> None:
    rows = [_row("niadra", "x1", "identity", ok=True), _row("niadra", "x2", "identity", ok=False, tokens=30)]
    (line,) = summarize(rows, {"x1"})
    assert line["cases"] == 1 and line["deterministic"] == 1.0
    assert line["by_category"]["identity"]["cases"] == 1
    assert line["tokens"]["default"]["median"] == 20


def test_cost_of_platform_plans_follows_the_tighter_quota(config) -> None:
    rows = cost.compute(
        config,
        tokens_per_turn={"niadra": 200, "mem0_oss": 300},
        mem0_seed_usage={},
        mem0_infer_adds=0,
        rerank_usage={},
        rerank_searches=0,
        platform_measured=False,
    )
    pro = next(r for r in rows if r["system"] == "mem0_platform" and r["variant"] == "pro")
    assert pro["conversations_per_month"] == 5000 and pro["memory_usd_per_1000"] == 49.8
    low = next(r for r in rows if r["variant"] == "price_low")
    # 200 tokens x 10 turns x 1000 conversations at 0.40 USD per million input tokens.
    assert low["memory_usd_per_1000"] == 5.0 and low["agent_prompt_usd_per_1000"] == 0.8


def test_mem0_model_spend_per_add_from_the_meter(config) -> None:
    before = {
        "models": {"google/gemini-2.5-flash-lite": {"prompt_tokens": 100, "completion_tokens": 0, "calls": 1}}
    }
    after = {
        "models": {
            "google/gemini-2.5-flash-lite": {
                "prompt_tokens": 1_000_100,
                "completion_tokens": 100_000,
                "calls": 101,
            }
        }
    }
    usage = cost.meter_delta(before, after)
    rows = cost.compute(
        config,
        tokens_per_turn={},
        mem0_seed_usage=usage,
        mem0_infer_adds=100,
        rerank_usage={},
        rerank_searches=0,
        platform_measured=False,
    )
    oss = next(r for r in rows if r["system"] == "mem0_oss")
    # (1M x 0.10 + 0.1M x 0.40) / 1M = 0.14 USD for 100 adds; 10 adds per conversation.
    assert oss["usd_per_add"] == 0.0014 and oss["memory_usd_per_1000"] == 14.0


def _case(cases, category: str):
    return next(c for c in cases if c.category == category)


def test_a_count_in_a_date_is_not_the_answer_but_a_stated_count_is(cases) -> None:
    case = _case(cases, "recurrence")
    count = next(alt for alt in case.expect.all_of[0] if alt.isdigit())
    dated = f"Conversa de 0{count}/09 e {count} de setembro; registro 21889."
    assert matches_all(dated, case.expect.all_of)  # the first run's rule counted it
    assert not context_holds_answer(dated, case)
    assert context_holds_answer(f"Do histórico: {count} reclamações nos últimos 90 dias.", case)
    assert context_holds_answer(f"Complained {count} times in 90 days.", case)


def test_a_count_holds_when_every_occurrence_is_listed_by_its_reference(cases) -> None:
    case = _case(cases, "recurrence")
    refs = [
        t
        for s in case.sessions
        if s.role == "key"
        for turn in s.turns
        for t in turn.text.replace(".", " ").split()
        if t.isdigit() and len(t) >= 3
    ]
    listed = "; ".join(f"incident {r} on 12/09" for r in refs)
    assert context_holds_answer(listed, case)
    assert not context_holds_answer(listed.rsplit(";", 1)[0], case)


def test_a_deadline_day_holds_only_next_to_a_word_that_sets_it(cases) -> None:
    case = _case(cases, "continuity")
    day = case.expect.all_of[0][0]
    assert not context_holds_answer(f"Conversa de {day}/09 no WhatsApp.", case)
    assert context_holds_answer(f"Cliente precisa da solução até o dia {day}.", case)
    assert context_holds_answer(f"Needs it solved by the {day}th.", case)


def test_values_of_three_or_more_digits_still_hold_as_whole_tokens(cases) -> None:
    case = _case(cases, "identity")
    protocol = case.expect.all_of[0][0]
    assert context_holds_answer(f"protocolo {protocol} enviado", case)
    assert not context_holds_answer(f"protocolo {protocol}9 enviado", case)


def test_unanswerable_cases_are_judged_with_the_no_record_rubric(cases) -> None:
    v2 = generate.load(bench_config.dataset_dir("v2"))
    case = next(c for c in v2 if c.category == "unanswerable" and c.expect.none_of)
    prompt = judge_prompt(case, "x")
    assert case.expect.no_record in prompt and case.expect.none_of[0] in prompt
    assert "no record of" in prompt
    plain = judge_prompt(cases[0], "x")
    assert plain == JUDGE_PROMPT.format(
        question=cases[0].probe.question,
        reference=cases[0].expect.reference_answer,
        required=" and ".join(" or ".join(g) for g in cases[0].expect.all_of),
        forbidden="none",
        answer="x",
    )
