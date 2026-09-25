import json

import httpx
from niadra_mock import MOCK_KEY, MockApp

from niadra_bench.dataset.model import CATEGORIES
from niadra_bench.runner import METRICS, Options, Run
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
    assert set(summary["metrics"]) == {"latency", "accuracy", "tokens", "privacy", "cost", "resilience"}
    systems = {r["system"] for r in summary["metrics"]["accuracy"]["results"]}
    assert systems == {"niadra", "no_memory", "full_history"}
    niadra = next(r for r in summary["metrics"]["accuracy"]["results"] if r["system"] == "niadra")
    assert len(niadra["deterministic"]["runs"]) == 2
    assert set(niadra["by_category"]) == set(CATEGORIES)
    privacy = next(r for r in summary["metrics"]["privacy"]["results"] if r["system"] == "niadra")
    assert privacy["leaks"]["median"] == 0  # the mock withholds the V2 session from a V0 read
    resilience = {r["fault"]: r for r in summary["metrics"]["resilience"]["results"]}
    assert resilience["delay_2000ms"]["raised_rate"]["median"] == 0.0
    assert resilience["delay_2000ms"]["within_budget_rate"]["median"] == 1.0
    rows = (out / "cases-rep1.jsonl").read_text().splitlines()
    assert len(rows) == 28 * 3 + 28  # three systems answer every case; Niadra also reads its second view
