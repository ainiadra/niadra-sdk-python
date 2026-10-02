"""Amazon Bedrock AgentCore Memory: the adapter's calls, the signing proxy, and a dry run of both
identity scenarios against the stand-in of the service."""

import json
from datetime import UTC, datetime

import httpx
from niadra_mock import MOCK_KEY, MockApp

from niadra_bench.identity import Identities
from niadra_bench.runner import Options, Run, load_cases
from niadra_bench.services.agentcore_proxy import AgentCoreProxy, strategy_inputs
from niadra_bench.services.fakes import FakeAgentCore
from niadra_bench.systems.agentcore_memory import (
    AgentCoreMemory,
    actor_id,
    exchange_events,
    session_id,
    settings,
)
from tests.conftest import word_tokenizer

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def test_ids_fit_what_the_api_takes() -> None:
    assert actor_id("cust-pt-001-abc123r1") == "cust-pt-001-abc123r1"
    assert actor_id("phone:+5511987654321") == "phone--5511987654321"
    assert actor_id("email:ana.lima.9f3a2c@bench.niadra.com") == "email-ana-lima-9f3a2c-bench-niadra-com"
    long = session_id("email-" + "a" * 120, "s1")
    assert len(long) == 100 and long != session_id("email-" + "a" * 120, "s2")


def test_a_conversation_is_one_event_per_exchange_and_a_record_is_ingested(cases) -> None:
    case = next(c for c in cases if any(s.record for s in c.sessions))
    ids = Identities.for_case(case, "t1")
    session = next(s for s in case.sessions if len(s.turns) >= 4)
    events = exchange_events(session, NOW)
    assert sum(len(payload) for _, payload in events) == len(session.turns)
    assert all(payload[0]["conversational"]["role"] in ("USER", "ASSISTANT") for _, payload in events)
    assert [at for at, _ in events] == sorted(at for at, _ in events)
    known = AgentCoreMemory(url="http://x", now=NOW)
    calls = known.seed_calls(case, ids)
    ingests = [c for c in calls if c.path.endswith("/ingest")]
    assert len(ingests) == sum(1 for s in case.sessions if s.record)
    assert "json" in ingests[0].json["source"]["inline"]["payload"][0]
    assert ingests[0].json["contentTimestamp"] < NOW.timestamp()
    assert {c.json["actorId"] for c in calls} == {known.customer(ids)}
    # each channel's own id: one actor per kind of handle, and the read uses the probe channel's
    apart = AgentCoreMemory(url="http://x", now=NOW, scenario="per_channel_id")
    actors = {c.json["actorId"] for c in apart.seed_calls(case, ids)}
    assert len(actors) > 1 and known.customer(ids) not in actors
    read = apart.read_call(case, ids, "q")
    assert read.json["namespacePath"] == f"/customers/{apart.actor(ids, case.probe.channel)}/"
    assert read.json["searchCriteria"] == {"searchQuery": "q", "topK": settings()["read"]["top_k"]}


def test_the_resource_has_the_three_built_in_strategies_under_one_root() -> None:
    memory = settings()["memory"]
    inputs = strategy_inputs(memory["strategies"], memory["namespace_root"])
    assert [next(iter(i)) for i in inputs] == [
        "semanticMemoryStrategy",
        "userPreferenceMemoryStrategy",
        "summaryMemoryStrategy",
    ]
    templates = [next(iter(i.values()))["namespaceTemplates"][0] for i in inputs]
    assert all(t.startswith("/customers/{actorId}/") for t in templates) and "{sessionId}" in templates[2]


async def test_the_proxy_signs_for_the_region_and_counts_what_aws_bills(monkeypatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    seen: list[httpx.Request] = []

    def aws(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/retrieve"):
            return httpx.Response(429, json={"message": "slow down"})
        return httpx.Response(201, json={"event": {}})

    proxy = AgentCoreProxy(region="us-east-2", upstream="aws", transport=httpx.MockTransport(aws))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://p") as client:
        assert (await client.post("/memories/m-1/events", json={"actorId": "a"})).status_code == 201
        assert (await client.post("/memories/m-1/retrieve", json={})).status_code == 429
        meter = (await client.get("/_meter")).json()
        assert (await client.delete("/_bench/memory/m-other")).status_code == 404
    assert seen[0].url.host == "bedrock-agentcore.us-east-2.amazonaws.com"
    assert "/us-east-2/bedrock-agentcore/aws4_request" in seen[0].headers["authorization"]
    assert meter["requests"]["events"] == 1 and meter["requests"]["throttled"] == 1
    assert meter["requests"]["retrievals"] == 0 and meter["usd"] == 0.0003


async def test_the_proxy_refuses_past_the_runs_ceiling() -> None:
    fake = FakeAgentCore()
    fake.memories["m-1"] = {"id": "m-1", "status": "ACTIVE", "strategies": []}
    proxy = AgentCoreProxy(upstream="http://fake", transport=httpx.ASGITransport(app=fake), max_usd=0.0005)
    event = {"actorId": "a", "sessionId": "s", "payload": [{"conversational": {"content": {"text": "x"}}}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://p") as client:
        statuses = [(await client.post("/memories/m-1/events", json=event)).status_code for _ in range(3)]
        opened = await client.get("/memories/m-1/memoryRecord/mem-0")
    assert statuses == [201, 201, 402] and opened.status_code == 200
    assert proxy.counters["refused"] == 1 and proxy.counters["events"] == 2


async def test_a_dry_run_measures_agentcore_in_both_identity_scenarios(config, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NIADRA_API_KEY", MOCK_KEY)
    monkeypatch.delenv("NIADRA_BOOTSTRAP", raising=False)
    monkeypatch.delenv("MEM0_METER_URL", raising=False)
    monkeypatch.setattr(AgentCoreMemory, "settle_poll_s", 0.0)
    monkeypatch.setattr(AgentCoreMemory, "settle_quiet_s", 0.0)
    monkeypatch.setattr(AgentCoreMemory, "fresh_poll_s", 0.01)
    mock = MockApp()
    fake = FakeAgentCore()
    proxy = AgentCoreProxy(upstream="http://fake", transport=httpx.ASGITransport(app=fake), poll_s=0.0)
    options = Options(
        systems={"niadra", "agentcore_memory"},
        metrics={"latency", "accuracy", "tokens", "privacy", "cost", "history", "ingest", "freshness"},
        repetitions=1,
        dry_run=True,
        limit=14,
        quick=True,
        output=tmp_path,
        dataset="v2",
        niadra_transport=lambda: httpx.ASGITransport(app=mock.asgi),
        niadra_base_url="http://niadra-mock",
        system_transports={"agentcore_memory": httpx.ASGITransport(app=proxy)},
        extra={"tokenizer": word_tokenizer},
    )
    out = await Run(config, load_cases("v2"), options).execute()
    summary = json.loads((out / "summary.json").read_text())
    metrics = summary["metrics"]
    scenarios = {r["scenario"] for r in metrics["accuracy"]["results"] if r["system"] == "agentcore_memory"}
    assert scenarios == {"known_id", "per_channel_id"}
    assert all(
        r["retrieve_errors"] == 0 for r in metrics["accuracy"]["results"] if r["system"] == "agentcore_memory"
    )
    # one line per timed metric, from the known id instance
    latency = [r for r in metrics["latency"]["results"] if r["system"] == "agentcore_memory"]
    assert len(latency) == 1 and latency[0]["path"] == "region"
    operations = {(r["system"], r["operation"]) for r in metrics["history"]["results"]}
    assert {("agentcore_memory", "search"), ("agentcore_memory", "open")} <= operations
    fresh = {r["system"]: r for r in metrics["freshness"]["results"]}
    assert fresh["agentcore_memory"]["timeouts"] == 0
    prices = settings()["prices"]
    cost = [r for r in metrics["cost"]["results"] if r["system"] == "agentcore_memory"]
    assert len(cost) == 1 and cost[0]["variant"] == "service_price"
    assert cost[0]["memory_usd_per_1000"]["median"] == (prices["event"] + prices["retrieval"]) * 10 * 1000
    rep = json.loads((out / "rep-1.json").read_text())
    settle = rep["settle"]["agentcore_memory:known_id"]
    assert settle["settled"] is True and settle["sessions_summarized"] == settle["sessions_written"]
    assert settle["configuration"]["strategies"] == ["semantic", "user_preference", "summary"]
    line = next(r for r in rep["cost"] if r["system"] == "agentcore_memory")
    assert line["storage_usd_per_1000_per_month"] > 0
    # the run's two memories are deleted when it ends
    assert fake.memories == {} and proxy.memories == {}
    assert proxy.counters["events"] > 0 and proxy.counters["ingests"] > 0
