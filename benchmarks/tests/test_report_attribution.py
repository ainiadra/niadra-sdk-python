"""Loss attribution (estudo 17, WP-11): each Niadra probe's exclusion manifest in its row, lost answers by
cause, and Mem0's two identity conditions side by side in summary.md."""

import json

import httpx
import pytest

from niadra_bench import report_summary
from niadra_bench.metrics import exclusions
from niadra_bench.metrics.accuracy import CaseRow
from niadra_bench.targets.niadra import Keys, NiadraTarget

HELD = {"id": "a1", "kind": "episode", "category": "billing", "reason": "verification"}
EXCLUDED = [
    {**HELD, "rule": "min_verification", "evidence_event_ids": ["e1", "e2"]},
    {"id": "a2", "kind": "fact", "category": "orders", "reason": "budget", "rule": "skeleton"},
    {"id": "a3", "kind": "fact", "category": "orders", "reason": "budget", "rule": "skeleton"},
]


def _row(case_id: str, category: str, judge: bool | None, meta: dict | None = None, system: str = "niadra"):
    return CaseRow(
        repetition=1, system=system, scenario=None, case_id=case_id, language="pt", category=category,
        view="voice", purpose="answer", tokens=0, retrieve_ms=0.0, error=None, context_has_answer=None,
        context_has_answer_loose=None, leak=None, answer="x", deterministic=judge, judge=judge,
        judge_reason=None, meta=meta if meta is not None else {},
    )  # fmt: skip


def test_a_manifest_keeps_ids_reasons_and_rules_never_evidence() -> None:
    got = exclusions.compact("r1", EXCLUDED)
    assert got["withheld"] == 1
    assert got["by_reason"] == {"budget/skeleton": 2, "verification/min_verification": 1}
    assert got["items"][0] == {**HELD, "rule": "min_verification"}


def test_a_lost_answer_is_withheld_not_withheld_or_unknown_by_its_manifest() -> None:
    held = {"exclusions": exclusions.compact("r1", EXCLUDED)}
    clear = {"exclusions": exclusions.compact("r2", EXCLUDED[1:])}
    rows = [
        _row("c1", "identity", False, held),
        _row("c2", "identity", False, clear),
        _row("c3", "paraphrase", False),
        _row("c4", "paraphrase", True, clear),
        _row("c5", "paraphrase", False, held),  # not a valid case
        _row("c1", "identity", False, held, system="mem0_oss"),
    ]
    got = exclusions.attribute(rows, {"c1", "c2", "c3", "c4"})
    assert (got["answers"], got["with_manifest"]) == (4, 3)
    assert got["lost"] == {"withheld": 1, "not_withheld": 1, "unknown": 1}
    assert got["lost_by_category"]["identity"] == {"withheld": 1, "not_withheld": 1}


def _governance(receipts: list[dict], lineage: dict, status: int = 200):
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer admin-key"
        if status != 200:
            return httpx.Response(status)
        if request.url.path == "/v1/receipts/search":
            assert json.loads(request.content)["conversation_id"] == "bench-t-pt-001-probe-1"
            return httpx.Response(200, json={"items": receipts, "next_cursor": None})
        return httpx.Response(200, json=lineage)

    return httpx.MockTransport(answer), calls


async def test_the_manifest_comes_from_the_probes_read_receipt_and_its_lineage() -> None:
    receipts = [
        {"receipt_id": "old", "manifest_hash": "h0", "read_at": "2026-09-27T12:00:00Z"},
        {"receipt_id": "new", "manifest_hash": "h1", "read_at": "2026-09-27T12:00:05Z"},
        {"receipt_id": "none", "manifest_hash": None, "read_at": "2026-09-27T12:00:09Z"},
    ]
    transport, calls = _governance(receipts, {"manifest_found": True, "excluded": EXCLUDED})
    async with httpx.AsyncClient(transport=transport) as http:
        got = await exclusions.fetch(http, "http://cell", {"authorization": "Bearer admin-key"},
                                     "bench-t-pt-001-probe-1")  # fmt: skip
    assert got is not None and got["receipt_id"] == "new" and got["withheld"] == 1
    assert calls == ["/v1/receipts/search", "/v1/lineage/receipt/new"]
    transport, _ = _governance([], {})
    async with httpx.AsyncClient(transport=transport) as http:
        headers = {"authorization": "Bearer admin-key"}
        assert await exclusions.fetch(http, "http://cell", headers, "bench-t-pt-001-probe-1") is None
    transport, _ = _governance([], {}, status=403)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(exclusions.GovernanceUnavailableError, match="403"):
            await exclusions.fetch(http, "http://cell", headers, "bench-t-pt-001-probe-1")


async def test_the_target_annotates_its_rows_with_the_admin_key_of_the_bootstrap() -> None:
    receipts = [{"receipt_id": "r", "manifest_hash": "h", "read_at": "2026-09-27T12:00:00Z"}]
    transport, _ = _governance(receipts, {"manifest_found": True, "excluded": EXCLUDED})
    keys = Keys({"whatsapp": "k-wa"}, {"keys": {"whatsapp": "k-wa"}, "admin_key": "admin-key"})
    target = NiadraTarget(keys, base_url="http://cell", transport_factory=lambda: transport)
    await target.start()
    rows = [
        _row("pt-001", "identity", False, {"conversation": "bench-t-pt-001-probe-1"}),
        _row("x", "x", True),
    ]
    report = await target.annotate(rows, wait_s=0)
    await target.close()
    assert report == {"recorded": 1, "rows": 2, "missing": 0}
    assert rows[0].meta["exclusions"]["withheld"] == 1
    # No admin credential (a run against the emulator): nothing is asked, the rows stay as they were.
    bare = NiadraTarget(
        Keys({"whatsapp": "k-wa"}), base_url="http://cell", transport_factory=lambda: transport
    )
    assert (await bare.annotate(rows))["unavailable"] == "no admin credential"


def test_summary_md_puts_mem0s_two_conditions_side_by_side() -> None:
    def line(system: str, scenario: str | None, judge: float) -> dict:
        share = {"median": judge}
        return {"system": system, "scenario": scenario, "judge": share, "deterministic": share,
                "by_category": {"identity": {"judge": share, "deterministic": share}}}  # fmt: skip

    results = [
        line("mem0_oss", "per_channel_id", 0.0),
        line("no_memory", None, 0.0),
        line("mem0_oss", "known_id", 1.0),
        line("niadra", None, 0.9),
    ]
    summary = {"run_id": "r", "environment": {"kind": "local"}, "metrics": {"accuracy": {"results": results}}}
    attribution = {"answers": 4, "with_manifest": 3, "lost": {"withheld": 1}, "lost_by_category": {
        "identity": {"withheld": 1}}}  # fmt: skip
    text = report_summary.markdown(summary, [{"exclusions": {"recorded": 3, "attribution": attribution}}])
    header = next(h for h in text.splitlines() if h.startswith("| Category | niadra"))
    assert header.split(" | ")[1:4] == ["niadra", "mem0_oss (shared id)", "mem0_oss (id per channel)"]
    assert "| identity | 90.0% | 100.0% | 0.0% | 0.0% |" in text
    assert "| all | 1 | 0 | 0 |" in text
