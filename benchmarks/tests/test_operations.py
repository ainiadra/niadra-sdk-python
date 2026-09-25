"""Metrics 8 (history navigation) and 9 (ingestion acknowledgement): what each side is sent, how an
answer is counted, and how the lines are summarized."""

import json
from collections import Counter
from pathlib import Path

import httpx
import pytest
from niadra_mock import MOCK_KEY, MockApp

from niadra_bench import config as bench_config
from niadra_bench.identity import Identities
from niadra_bench.metrics import history, ingest, operations
from niadra_bench.runner import aggregate
from niadra_bench.targets.mem0 import Mem0RestTarget
from niadra_bench.targets.niadra import Keys, NiadraTarget

PUBLISHED = bench_config.RESULTS_DIR / "2026-09-25-6efee4"


def _pairs(cases, n=2, tag="t1"):
    return [(case, Identities.for_case(case, tag)) for case in cases[:n]]


class FakeMem0:
    """Mem0's REST server as far as these metrics call it: search, read one memory, add."""

    def __init__(self, with_results: set[str]) -> None:
        self.with_results = with_results
        self.adds: list[dict] = []
        self.paths: Counter[str] = Counter()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths[f"{request.method} {request.url.path}"] += 1
        if request.url.path == "/search":
            user = json.loads(request.content)["filters"]["user_id"]
            found = [{"id": f"mem-{user}", "memory": "note"}] if user in self.with_results else []
            return httpx.Response(200, json={"results": found})
        if request.method == "GET" and request.url.path.startswith("/memories/"):
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1], "memory": "note"})
        if request.url.path == "/memories":
            self.adds.append(json.loads(request.content))
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404)


def _mem0(config) -> Mem0RestTarget:
    return Mem0RestTarget("known_id", config.mem0, url="http://mem0", api_key="k", configure=False)


async def test_mem0_history_searches_as_metric_1_and_opens_only_memories_a_search_returned(
    config, cases
) -> None:
    pairs = _pairs(cases)
    known = pairs[0][1].mem0_user("known_id", pairs[0][0].probe.channel)
    fake = FakeMem0({known})
    lines = await history.run(
        None,
        _mem0(config),
        pairs,
        "t1",
        config.history,
        config.mem0,
        rates=[20],
        duration_s=0.5,
        mem0_transport=lambda: httpx.MockTransport(fake),
    )
    search, opened = lines
    assert (search["operation"], search["call"]) == ("search", "POST /search")
    assert (opened["operation"], opened["call"]) == ("open", "GET /memories/{memory_id}")
    assert search["sent"] == 10 and search["errors"] == {}
    assert search["empty"] == 5  # the second user has no memory: fast, empty, counted apart
    assert opened["sent"] == 10 and opened["n"] == 10
    assert fake.paths[f"GET /memories/mem-{known}"] == opened["sent"] + len(pairs)  # warm-ups included
    assert {p for p in fake.paths if p.startswith("GET")} == {f"GET /memories/mem-{known}"}


async def test_mem0_open_is_skipped_but_written_when_no_search_returned_a_memory(config, cases) -> None:
    fake = FakeMem0(set())
    lines = await history.run(
        None,
        _mem0(config),
        _pairs(cases),
        "t1",
        config.history,
        config.mem0,
        rates=[20],
        duration_s=0.2,
        mem0_transport=lambda: httpx.MockTransport(fake),
    )
    opened = lines[1]
    assert opened["sent"] == 0 and opened["p95"] is None
    assert opened["skipped"] == "no memory returned by any search"


async def test_mem0_ingest_runs_raw_then_infer_with_one_new_exchange_per_call(config, cases) -> None:
    fake = FakeMem0(set())
    pairs = _pairs(cases)
    settings = config.ingest.model_copy(update={"turns_per_conversation": 3})
    lines = await ingest.run(
        None,
        _mem0(config),
        pairs,
        "t1",
        settings,
        rates=[20],
        duration_s=0.5,
        mem0_transport=lambda: httpx.MockTransport(fake),
    )
    assert [(x["operation"], x["call"]) for x in lines] == [
        ("add_raw", "POST /memories infer=false"),
        ("add_infer", "POST /memories"),
    ]
    per_mode = len(pairs) + 10  # warm-ups, then 20 per second for half a second
    raw, infer = fake.adds[:per_mode], fake.adds[per_mode:]
    assert len(infer) == per_mode
    assert all(a["infer"] is False for a in raw)
    assert all("infer" not in a for a in infer)  # Mem0's default: the extraction runs before the answer
    for adds in (raw, infer):
        assert all([m["role"] for m in a["messages"]] == ["user", "assistant"] for a in adds)
        assert len({a["messages"][0]["content"] for a in adds}) == len(adds)
        users = [a["user_id"] for a in adds]
        assert users[:3] == [pairs[0][1].mem0_user("known_id", "whatsapp")] * 3
        assert users[3] == pairs[1][1].mem0_user("known_id", "whatsapp")


async def test_niadra_ingest_sends_the_items_track_sends_each_once(config, cases) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 2, "duplicates": 0, "errors": []})

    pairs = _pairs(cases)
    op = ingest.niadra_operation("edge", "http://niadra", MOCK_KEY, pairs, "t1", 3)
    op.probe.transport = lambda: httpx.MockTransport(handler)
    [line] = await operations.measure([op], [20], 0.5, len(pairs), 5)
    assert (line["operation"], line["call"], line["sent"], line["errors"]) == (
        "batch",
        "POST /v1/batch",
        10,
        {},
    )
    items = [item for body in bodies for item in body["items"]]
    assert all(len(body["items"]) == 2 for body in bodies)
    assert len({i["idempotency_key"] for i in items}) == len(items)
    assert [i["speaker"]["role"] for i in bodies[0]["items"]] == ["customer", "ai_agent"]
    exchanges = Counter(body["items"][0]["conversation_id"] for body in bodies)
    assert set(exchanges.values()) <= {1, 2, 3}  # `turns_per_conversation` exchanges per conversation


async def test_niadra_ingest_against_the_mock_is_acknowledged(config, cases, monkeypatch) -> None:
    monkeypatch.delenv("NIADRA_CLUSTER_INGEST_URL", raising=False)
    mock = MockApp()
    target = NiadraTarget(
        Keys({"whatsapp": MOCK_KEY}),
        base_url="http://niadra-mock",
        transport_factory=lambda: httpx.ASGITransport(app=mock.asgi),
    )
    await target.start()
    try:
        lines = await ingest.run(
            target,
            None,
            _pairs(cases),
            "t1",
            config.ingest,
            rates=[20],
            duration_s=0.3,
            transport=lambda: httpx.ASGITransport(app=mock.asgi),
        )
    finally:
        await target.close()
    [line] = lines
    assert (line["system"], line["path"], line["sent"], line["errors"]) == ("niadra", "edge", 6, {})


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(200, json={"items": [{"id": "ep:1"}]}), "ok"),
        (httpx.Response(200, json={"items": []}), "empty"),
        (httpx.Response(200, json={"items": [{"id": "ep:1"}], "degraded": "text_only"}), "degraded"),
        (httpx.Response(404, json={}), "http404"),
        (httpx.Response(503, json={}), "http503"),
        (httpx.Response(200, content=b"<html>"), "unreadable"),
    ],
)
async def test_a_search_answer_is_counted_by_what_it_carried(response, outcome) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        _ms, got = await operations.timed(client.post("http://x/s"), 200, history.niadra_search_outcome)
    assert got == outcome


async def test_a_timeout_is_an_error_named_by_its_kind() -> None:
    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        _ms, got = await operations.timed(client.post("http://x/s"), 200)
    assert got == "ReadTimeout"


def _line(p95: float, errors: dict[str, int], sent: int = 100) -> dict:
    return {
        "system": "niadra",
        "path": "edge",
        "operation": "search",
        "call": "POST /v1/history/search",
        "rate": 10,
        "p50": p95 / 2,
        "p95": p95,
        "p99": p95 * 2,
        "max": p95 * 3,
        "sent": sent,
        "errors": errors,
        "empty": 1,
        "degraded": 2,
    }


def test_lines_are_summarized_across_repetitions_like_latency() -> None:
    reps = [
        {"history": [_line(40.0, {"http503": 2})]},
        {"history": [_line(60.0, {"ReadTimeout": 1, "http503": 1})]},
        {"history": []},
    ]
    summary = operations.aggregate(reps, "history")
    assert summary is not None and summary["unit"] == "ms"
    [row] = summary["results"]
    assert row["p95"] == {"median": 50.0, "min": 40.0, "max": 60.0, "runs": [40.0, 60.0, None]}
    assert (row["errors"], row["error_kinds"]) == (4, {"ReadTimeout": 1, "http503": 3})
    assert (row["sent"], row["empty"], row["degraded"]) == (200, 2, 4)
    assert operations.aggregate(reps, "ingest") is None


def test_a_run_without_the_new_metrics_summarizes_exactly_as_published() -> None:
    reps = [json.loads(p.read_text()) for p in sorted(Path(PUBLISHED).glob("rep-*.json"))]
    published = json.loads((PUBLISHED / "summary.json").read_text())
    assert aggregate(reps) == published["metrics"]
    assert "history" not in aggregate(reps) and "ingest" not in aggregate(reps)
