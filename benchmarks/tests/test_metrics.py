from niadra_bench.metrics import cost
from niadra_bench.metrics.accuracy import CaseRow, summarize, valid_cases


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
