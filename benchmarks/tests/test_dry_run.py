import json

import httpx
from niadra_mock import MOCK_KEY, MockApp

from niadra_bench.dataset.model import BASE_CATEGORIES, V2_CATEGORIES
from niadra_bench.runner import METRICS, Options, Run, load_cases
from tests.conftest import word_tokenizer


async def test_a_dry_run_against_niadra_mock_writes_a_complete_summary(
    config, cases, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("NIADRA_API_KEY", MOCK_KEY)
    monkeypatch.delenv("NIADRA_BOOTSTRAP", raising=False)
    monkeypatch.delenv("MEM0_METER_URL", raising=False)
    mock = MockApp()
    options = Options(
        systems={"niadra"},
        metrics=set(METRICS) - {"freshness"},
        repetitions=2,
        dry_run=True,
        limit=28,
        quick=True,
        output=tmp_path,
        niadra_transport=lambda: httpx.ASGITransport(app=mock.asgi),
        niadra_base_url="http://niadra-mock",
        extra={"tokenizer": word_tokenizer},
    )
    out = await Run(config, cases, options).execute()
    summary = json.loads((out / "summary.json").read_text())
    assert summary["schema"] == "niadra-bench.results.v1"
    assert summary["environment"]["kind"] == "dry-run" and summary["environment"]["region"] is None
    assert summary["config"]["agent"] == "context_only"
    assert set(summary["metrics"]) == {
        "latency",
        "accuracy",
        "tokens",
        "privacy",
        "cost",
        "resilience",
        "history",
        "ingest",
        "backing",
    }
    systems = {r["system"] for r in summary["metrics"]["accuracy"]["results"]}
    assert systems == {"niadra", "no_memory", "full_history"}
    niadra = next(r for r in summary["metrics"]["accuracy"]["results"] if r["system"] == "niadra")
    assert len(niadra["deterministic"]["runs"]) == 2
    assert set(niadra["by_category"]) == set(BASE_CATEGORIES)
    assert summary["dataset"]["version"] == "v1" and summary["config"]["niadra_memory_v2"] == "unchanged"
    privacy = next(r for r in summary["metrics"]["privacy"]["results"] if r["system"] == "niadra")
    assert privacy["leaks"]["median"] == 0  # the mock withholds the V2 session from a V0 read
    resilience = {r["fault"]: r for r in summary["metrics"]["resilience"]["results"]}
    assert resilience["delay_2000ms"]["raised_rate"]["median"] == 0.0
    assert resilience["delay_2000ms"]["within_budget_rate"]["median"] == 1.0
    navigation = {r["operation"]: r for r in summary["metrics"]["history"]["results"]}
    assert set(navigation) == {"search", "open"}
    assert all(r["errors"] == 0 and r["sent"] > 0 and "skipped" not in r for r in navigation.values())
    [ack] = summary["metrics"]["ingest"]["results"]
    assert (ack["operation"], ack["errors"]) == ("batch", 0) and ack["sent"] > 0
    # The context-only agent answers with the block itself, so every value it states has a source.
    backed = {r["system"]: r for r in summary["metrics"]["backing"]["results"]}
    assert backed["niadra"]["answers"] == 56 and backed["niadra"]["unbacked"] == 0
    rows = (out / "cases-rep1.jsonl").read_text().splitlines()
    assert len(rows) == 28 * 3 + 28  # three systems answer every case; Niadra also reads its second view


async def test_a_dry_run_on_dataset_v2_scores_every_new_category(config, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NIADRA_API_KEY", MOCK_KEY)
    monkeypatch.delenv("NIADRA_BOOTSTRAP", raising=False)
    monkeypatch.delenv("MEM0_METER_URL", raising=False)
    mock = MockApp()
    cases = load_cases("v2")
    # Two of each new category, one per language, besides a few v1 cases.
    picked = [c for c in cases if c.category not in V2_CATEGORIES][::60]
    for category in V2_CATEGORIES:
        for lang in ("pt", "en"):
            picked.append(next(c for c in cases if c.category == category and c.language == lang))
    options = Options(
        systems={"niadra"},
        metrics={"accuracy", "tokens", "privacy"},
        repetitions=1,
        dry_run=True,
        output=tmp_path,
        dataset="v2",
        niadra_transport=lambda: httpx.ASGITransport(app=mock.asgi),
        niadra_base_url="http://niadra-mock",
        extra={"tokenizer": word_tokenizer},
    )
    out = await Run(config, picked, options).execute()
    summary = json.loads((out / "summary.json").read_text())
    assert summary["dataset"]["version"] == "v2" and summary["dataset"]["generator_version"] == "2"
    assert summary["config"]["hash"] != "" and summary["config"]["context_has_answer_rule"] == "specific-v2"
    for line in summary["metrics"]["accuracy"]["results"]:
        assert set(V2_CATEGORIES) <= set(line["by_category"])
        assert "context_has_answer_loose" in line
    rows = [json.loads(r) for r in (out / "cases-rep1.jsonl").read_text().splitlines()]
    long_rows = [r for r in rows if r["category"] == "long_history" and r["system"] == "full_history"]
    assert long_rows and all(r["tokens"] > 300 for r in long_rows)
