import json
from dataclasses import replace
from pathlib import Path

import pytest

from niadra_bench import ab, report_ab
from niadra_bench.cli import main
from niadra_bench.metrics.accuracy import CaseRow
from niadra_bench.stats import across


def _metrics(judge: list[float], tokens: list[float], p95: list[float]) -> dict:
    share = across(judge, 4)
    niadra = {
        "system": "niadra",
        "scenario": None,
        "judge": share,
        "deterministic": share,
        "context_has_answer": share,
        "by_category": {"identity": {"judge": share, "deterministic": share}},
    }
    reference = {"system": "no_memory", "scenario": None, "judge": across([0.0], 4)}
    view = {"system": "niadra", "scenario": None, "view": "voice"}
    line = {"system": "niadra", "path": "edge", "rate": 10}
    return {
        "accuracy": {"results": [niadra, reference]},
        "tokens": {"results": [view | {"median": across(tokens, 1), "p95": across(tokens, 1)}]},
        "latency": {"results": [line | {"p50": across(p95, 1), "p95": across(p95, 1)}]},
    }


def test_the_delta_of_two_identical_sides_is_zero_in_every_cell() -> None:
    side = _metrics([0.5, 0.6, 0.7], [120, 121, 122], [30.0, 31.0, 29.0])
    cells = report_ab.cells(side, json.loads(json.dumps(side)))
    assert cells and all(c["delta"] == 0 for c in cells)
    assert {c["section"] for c in cells} == {"accuracy", "tokens", "latency"}
    # Only Niadra's lines are compared; the references are the validity rule's, not a side's.
    assert all(c["line"] != "no_memory" for c in cells)


def test_one_changed_cell_shows_up_with_its_sign() -> None:
    base = _metrics([0.5, 0.6, 0.7], [120, 121, 122], [30.0, 31.0, 29.0])
    cand = _metrics([0.5, 0.6, 0.7], [110, 111, 112], [30.0, 31.0, 29.0])
    changed = [c for c in report_ab.cells(base, cand) if c["delta"]]
    assert [(c["section"], c["field"], c["delta"]) for c in changed] == [
        ("tokens", "median", -10.0),
        ("tokens", "p95", -10.0),
    ]


def _row(case_id: str, *, tokens: int = 100, has: bool = True, verdict: bool = True, rep: int = 1) -> CaseRow:
    return CaseRow(
        repetition=rep, system="niadra", scenario=None, case_id=case_id, language="pt", category="identity",
        view="voice", purpose="answer", tokens=tokens, retrieve_ms=1.0, error=None, context_has_answer=has,
        context_has_answer_loose=has, leak=None, answer="x", deterministic=verdict, judge=None,
        judge_reason=None, meta={},
    )  # fmt: skip


def test_determinism_passes_on_identical_rows_and_lists_every_row_that_differs() -> None:
    rows = {1: [_row("pt-001"), _row("pt-002")]}
    side = _metrics([0.5], [120], [30.0])
    same = report_ab.determinism(report_ab.cells(side, side), rows, {1: list(rows[1])})
    assert same["passed"] and same["differences"] == []

    other = {1: [_row("pt-001"), replace(_row("pt-002"), tokens=101)]}
    cand = _metrics([0.5], [121], [12.0])
    verdict = report_ab.determinism(report_ab.cells(side, cand), rows, other)
    assert not verdict["passed"]
    assert any("tokens voice median" in d for d in verdict["differences"])
    assert any("pt-002 voice tokens: 100 != 101" in d for d in verdict["differences"])
    # Latency may differ: it is not a difference.
    assert not any("latency" in d for d in verdict["differences"])


def test_flips_list_the_valid_cases_that_changed_side() -> None:
    base = {1: [_row("pt-001", verdict=False, has=False), _row("pt-002"), _row("pt-003", verdict=False)]}
    cand = {1: [_row("pt-001"), _row("pt-002", verdict=False), _row("pt-003")]}
    out = report_ab.flips(base, cand, {1: {"pt-001", "pt-002"}})
    assert out["answer"] == {"identity": {"gained": ["pt-001@1"], "lost": ["pt-002@1"]}}
    assert out["context"] == {"identity": {"gained": ["pt-001@1"], "lost": []}}


def test_the_baseline_gets_the_default_of_every_key_only_the_candidate_sets() -> None:
    base, cand = ab.sides_env({}, {"NIADRA_MEMORY_V2": "on"}, same=False)
    assert base == {"NIADRA_MEMORY_V2": "off"} and cand == {"NIADRA_MEMORY_V2": "on"}
    base, cand = ab.sides_env({"NIADRA_MEMORY_V2": "on"}, {"NIADRA_SEMANTIC_CHANNEL": "models"}, same=False)
    assert base == {"NIADRA_MEMORY_V2": "on", "NIADRA_SEMANTIC_CHANNEL": "off"}
    assert cand == {"NIADRA_MEMORY_V2": "on", "NIADRA_SEMANTIC_CHANNEL": "models"}
    with pytest.raises(ab.AbError, match="no known default"):
        ab.sides_env({}, {"NIADRA_LINKED_WEIGHT": "1.0"}, same=False)
    with pytest.raises(ab.AbError, match="needs --candidate-env"):
        ab.sides_env({}, {}, same=False)
    with pytest.raises(ab.AbError, match="itself"):
        ab.sides_env({}, {"NIADRA_MEMORY_V2": "on"}, same=True)
    with pytest.raises(ab.AbError, match="KEY=VALUE"):
        ab.parse_env(["NIADRA_MEMORY_V2"])


def test_a_process_setting_is_refused_where_the_read_deployment_serves_production(cases) -> None:
    options = ab.AbOptions(baseline_env={}, candidate_env={"NIADRA_SEMANTIC_CHANNEL": "models"})
    with pytest.raises(ab.AbError, match="also serves production"):
        ab.Ab(cases, options)
    # The space setting is the harness's to change through the control API.
    ab.Ab(cases, ab.AbOptions(baseline_env={}, candidate_env={"NIADRA_MEMORY_V2": "on"}))
    with pytest.raises(ab.AbError, match="only --same"):
        ab.Ab(cases, ab.AbOptions(baseline_env={}, candidate_env={"NIADRA_MEMORY_V2": "on"}, mock=True))


def test_in_the_region_each_side_sets_the_space_flag_and_seeds_its_own_customers(cases) -> None:
    runner = ab.Ab(cases, ab.AbOptions(baseline_env={}, candidate_env={"NIADRA_MEMORY_V2": "on"}))
    base, cand = runner._run_for(runner.baseline), runner._run_for(runner.candidate)
    assert (base.options.memory_v2, cand.options.memory_v2) == (False, True)
    assert base.options.tag != cand.options.tag
    assert base.options.niadra_now == cand.options.niadra_now == runner.now
    assert base.options.references and not cand.options.references
    assert not base.options.dry_run and runner.kind != "local-cell"


def test_a_branch_against_main_runs_the_candidate_on_its_own_checkout(cases, tmp_path) -> None:
    with pytest.raises(ab.AbError, match="beside --local-cell"):
        ab.Ab(cases, ab.AbOptions(baseline_env={}, candidate_env={}, candidate_cell=tmp_path))
    runner = ab.Ab(
        cases,
        ab.AbOptions(
            baseline_env={"NIADRA_MEMORY_V2": "on"},
            candidate_env={},
            local_cell=tmp_path / "main",
            candidate_cell=tmp_path / "branch",
        ),
    )
    # The code is the difference: both sides keep the baseline's settings.
    assert runner.baseline.env == runner.candidate.env == {"NIADRA_MEMORY_V2": "on"}
    assert runner.kind == "local-cell" and "local" in runner.out.parts


def test_a_local_cell_runs_in_the_checkout_with_only_its_side_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NIADRA_MEMORY_V2", "on")
    monkeypatch.setenv("NIADRA_BASE_URL", "http://elsewhere")
    from datetime import UTC, datetime

    cell = ab.LocalCell(tmp_path, {"NIADRA_SEMANTIC_CHANNEL": "models"}, datetime(2026, 9, 25, tzinfo=UTC),
                        ["credit", "refund"], tmp_path / "side")  # fmt: skip
    env = cell.process_env()
    assert env["NIADRA_SEMANTIC_CHANNEL"] == "models"
    assert "NIADRA_MEMORY_V2" not in env and "NIADRA_BASE_URL" not in env
    command = cell.command()
    assert command[command.index("--project") + 1] == str(tmp_path)
    assert command[command.index("--operations") + 1] == "credit,refund"
    assert Path(command[command.index("python") + 1]).name == "cell_server.py"


def test_a_candidate_config_lays_its_sections_over_the_frozen_file(tmp_path) -> None:
    override = tmp_path / "candidate.toml"
    override.write_text("[niadra]\nviews = ['voice']\n")
    config, digest = ab.candidate_config(override)
    assert config.niadra.views == ["voice"] and config.run.repetitions == 3
    assert "+" in digest


def test_bench_ab_same_against_the_emulator_passes_with_a_zero_delta(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["ab", "--same", "--mock", "--limit", "14", "--quick", "--repetitions", "1",
              "--metrics", "accuracy,tokens,privacy,cost,latency", "--output", str(tmp_path)])  # fmt: skip
    assert exit_.value.code == 0
    [out] = list(tmp_path.iterdir())
    document = json.loads((out / "ab.json").read_text())
    assert document["schema"] == "niadra-bench.ab.v1" and document["mode"] == "same"
    assert document["environment"]["kind"] == "dry-run" and document["environment"]["publishable"] is False
    assert document["determinism"]["passed"]
    exact = [c for c in document["delta"] if c["exact"]]
    assert exact and all(c["delta"] == 0 for c in exact)
    assert {c["section"] for c in exact} >= {"accuracy", "tokens", "privacy"}
    # Never a file the site's importer would take.
    assert not list(out.rglob("summary.json"))
    assert (out / "baseline" / "cases-rep1.jsonl").exists() and (out / "candidate" / "rep-1.json").exists()
    assert "Determinism: passed" in capsys.readouterr().out
