from collections import Counter

from niadra_bench import config as bench_config
from niadra_bench.dataset import generate
from niadra_bench.dataset.model import Case, Session, Turn
from niadra_bench.dataset.validate import structural_problems
from niadra_bench.text import render_history


def test_generation_is_deterministic_and_matches_the_committed_dataset(config, cases) -> None:
    fresh = generate.generate(config.dataset)
    assert [c.model_dump_json() for c in fresh] == [c.model_dump_json() for c in cases]
    assert (
        generate.dataset_hash(bench_config.DATASET_DIR).removeprefix("sha256:")
        == (__import__("json").loads((bench_config.DATASET_DIR / "manifest.json").read_text())["sha256"])
    )


def test_sizes_and_proportions(config, cases) -> None:
    assert len(cases) == config.dataset.cases_per_language * len(config.dataset.languages) == 240
    counts = Counter((c.language, c.category) for c in cases)
    for lang in config.dataset.languages:
        for category, wanted in config.dataset.categories.items():
            assert counts[(lang, category)] == wanted
    assert all(config.dataset.min_sessions <= len(c.sessions) <= config.dataset.max_sessions for c in cases)
    assert {c.domain for c in cases} == {"telecom", "insurance", "banking", "retail", "logistics"}


def test_every_case_passes_the_structural_validity_rule(cases) -> None:
    assert {c.id: p for c in cases if (p := structural_problems(c))} == {}


def test_no_em_or_en_dash_and_no_real_email_domains(cases) -> None:
    text = "\n".join(render_history(c) + c.probe.question for c in cases)
    assert "\u2014" not in text and "\u2013" not in text
    assert "example.com" not in text


def _with(case: Case, **changes) -> Case:
    return case.model_copy(update=changes)


def test_the_rule_rejects_a_question_that_gives_the_answer_away(cases) -> None:
    case = next(c for c in cases if c.category == "identity")
    value = case.expect.all_of[0][0]
    leaked = _with(case, probe=case.probe.model_copy(update={"question": f"Is it {value}?"}))
    assert any("gives away" in p for p in structural_problems(leaked))


def test_the_rule_rejects_an_answer_that_also_sits_in_a_filler(cases) -> None:
    case = next(c for c in cases if c.category == "continuity")
    value = case.expect.all_of[0][0]
    filler = Session(
        id="sx", channel="email", days_ago=50, turns=[Turn(role="agent", text=f"Order {value} shipped.")]
    )
    assert any(
        "outside the key sessions" in p
        for p in structural_problems(_with(case, sessions=[*case.sessions, filler]))
    )


def test_the_rule_rejects_a_recurrence_count_that_does_not_match(cases) -> None:
    case = next(c for c in cases if c.category == "recurrence")
    fewer = [s for s in case.sessions if s.role != "key"] + [s for s in case.sessions if s.role == "key"][:1]
    assert any("reports" in p for p in structural_problems(_with(case, sessions=fewer)))


def test_privacy_cases_keep_the_value_in_one_verified_session(cases) -> None:
    for case in (c for c in cases if c.category == "privacy"):
        assert case.probe.verification == "V0"
        holders = [s for s in case.sessions if s.sensitive]
        assert len(holders) == 1
    case = next(c for c in cases if c.category == "privacy")
    unverified = [s.model_copy(update={"sensitive": False}) for s in case.sessions]
    assert structural_problems(_with(case, sessions=unverified))
