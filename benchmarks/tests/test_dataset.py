import hashlib
import json
from collections import Counter

import pytest

from niadra_bench import config as bench_config
from niadra_bench.dataset import generate
from niadra_bench.dataset.model import BASE_CATEGORIES, Case, Session, Turn
from niadra_bench.dataset.validate import structural_problems
from niadra_bench.text import render_history


def test_generation_is_deterministic_and_matches_the_committed_dataset(config, cases) -> None:
    fresh = generate.generate(config.dataset)
    assert [c.model_dump_json() for c in fresh] == [c.model_dump_json() for c in cases]
    assert (
        generate.dataset_hash(bench_config.DATASET_DIR).removeprefix("sha256:")
        == (json.loads((bench_config.DATASET_DIR / "manifest.json").read_text())["sha256"])
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


# Dataset v2


@pytest.fixture(scope="module")
def v2_settings(config):
    return bench_config.dataset_settings(config, "v2")


@pytest.fixture(scope="module")
def v2_cases():
    return generate.load(bench_config.dataset_dir("v2"))


def test_v2_is_deterministic_and_matches_its_committed_copy(v2_settings, v2_cases) -> None:
    fresh = generate.generate(v2_settings)
    assert [c.model_dump_json() for c in fresh] == [c.model_dump_json() for c in v2_cases]
    manifest = json.loads((bench_config.dataset_dir("v2") / "manifest.json").read_text())
    assert manifest["generator_version"] == "2"
    assert generate.dataset_hash(bench_config.dataset_dir("v2")) == "sha256:" + manifest["sha256"]


def test_v2_holds_every_v1_case_unchanged(cases, v2_cases) -> None:
    by_id = {c.id: c for c in v2_cases}
    assert all(by_id[c.id].model_dump_json() == c.model_dump_json() for c in cases)
    assert all(c.category in BASE_CATEGORIES for c in v2_cases if int(c.id[3:]) <= 120)


def test_v1_stays_the_dataset_of_the_published_run(config) -> None:
    published = json.loads((bench_config.RESULTS_DIR / "2026-09-25-6efee4" / "summary.json").read_text())
    assert generate.dataset_hash(bench_config.dataset_dir("v1")) == published["dataset"]["hash"]
    assert bench_config.dataset_settings(config, "v1") == config.dataset


def test_v1_config_hash_covers_the_files_it_always_did(config) -> None:
    digest = hashlib.sha256()
    for name in ("benchmark.toml", "mem0.config.json"):
        digest.update(name.encode())
        digest.update(hashlib.sha256((bench_config.CONFIG_DIR / name).read_bytes()).hexdigest().encode())
    assert (
        bench_config.config_hash() == bench_config.config_hash(dataset="v1") == "sha256:" + digest.hexdigest()
    )
    assert bench_config.config_hash(dataset="v2") != bench_config.config_hash(dataset="v1")


def test_v2_sizes_and_proportions(v2_settings, v2_cases) -> None:
    counts = Counter((c.language, c.category) for c in v2_cases)
    assert len(v2_cases) == v2_settings.cases_per_language * len(v2_settings.languages)
    for lang in v2_settings.languages:
        for category, wanted in v2_settings.categories.items():
            assert counts[(lang, category)] == wanted
    for case in v2_cases:
        if case.category == "long_history":
            assert 30 <= len(case.sessions) <= 60
            keys = [s for s in case.sessions if s.role == "key"]
            # The key session is not always among the latest: it sits anywhere in six months.
            assert all(10 <= s.days_ago <= 175 for s in keys)


def test_every_v2_case_passes_the_structural_validity_rule(v2_cases) -> None:
    assert {c.id: p for c in v2_cases if (p := structural_problems(c))} == {}
    text = "\n".join(render_history(c) + c.probe.question for c in v2_cases)
    assert "—" not in text and "–" not in text


def test_paraphrase_questions_share_no_content_word_with_the_statement(v2_cases) -> None:
    case = next(c for c in v2_cases if c.category == "paraphrase")
    key = next(s for s in case.sessions if s.role == "key")
    word = max((w for w in key.turns[0].text.split() if w.isalpha()), key=len)
    echoed = _with(case, probe=case.probe.model_copy(update={"question": f"{case.probe.question} {word}"}))
    assert any("shares content words" in p for p in structural_problems(echoed))


def test_count_by_topic_needs_the_other_matter_last_and_at_another_count(v2_cases) -> None:
    case = next(c for c in v2_cases if c.category == "recurrence_topic")
    latest = min((s for s in case.sessions if s.turns), key=lambda s: s.days_ago)
    assert latest.role == "other_topic"
    others = [s for s in case.sessions if s.role == "other_topic"]
    keys = [s for s in case.sessions if s.role == "key"]
    assert len(others) != len(keys)
    newest_key = keys[0].model_copy(update={"days_ago": 0.1, "id": "sz"})
    reordered = _with(case, sessions=[*case.sessions, newest_key])
    assert any("latest conversation" in p for p in structural_problems(reordered))


def test_unanswerable_cases_hold_no_value_for_what_they_ask(v2_cases) -> None:
    cases = [c for c in v2_cases if c.category == "unanswerable"]
    assert all(c.expect.no_record for c in cases)
    with_decoy = [c for c in cases if c.expect.none_of]
    assert 0 < len(with_decoy) < len(cases)
    case = with_decoy[0]
    key = next(s for s in case.sessions if s.role == "key")
    leaked = key.model_copy(
        update={"turns": [*key.turns, Turn(role="agent", text=f"Protocol {case.expect.none_of[0]}.")]}
    )
    sessions = [leaked if s.id == key.id else s for s in case.sessions]
    problems = structural_problems(_with(case, sessions=sessions))
    assert any("could pass for the missing one" in p for p in problems)
    assert any("sits in the session asked about" in p for p in problems)


def test_v1_serialization_has_no_v2_field(cases) -> None:
    assert all("no_record" not in c.model_dump_json() for c in cases)
