"""The systems added through adapters: the registry, the container entries, each adapter's calls, and a
dry run with one of them against an in-process fake of its documented routes."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from niadra_mock import MOCK_KEY, MockApp

from niadra_bench.identity import Identities
from niadra_bench.runner import BUILT_IN, SYSTEMS, Options, Run, load_cases
from niadra_bench.services.asgi import lifespan, read_body, respond_json
from niadra_bench.systems import REGISTRY
from niadra_bench.systems.ai_memory import AiMemory, cap_excerpt, session_items
from niadra_bench.systems.base import Call, HttpSystem
from niadra_bench.systems.graphiti import Graphiti, fact_line
from niadra_bench.systems.hindsight import Hindsight, retain_item
from niadra_bench.systems.memobase import Memobase
from niadra_bench.systems.memos import MemOS, found_memories
from niadra_bench.systems.supermemory import Supermemory
from tests.conftest import word_tokenizer

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _compose_keys() -> dict[str, str]:
    """Every system key a deploy/systems/*/compose.yaml names on its first line, and its directory."""
    out: dict[str, str] = {}
    for path in sorted((DEPLOY / "systems").glob("*/compose.yaml")):
        first = path.read_text().splitlines()[0]
        match = re.fullmatch(r"# systems: (.+)", first)
        assert match, f"{path} does not start with '# systems: <key> ...'"
        for key in match.group(1).split():
            assert key not in out, f"{key} is named by two compose files"
            out[key] = path.parent.name
    return out


def test_every_adapter_has_a_container_entry_and_every_entry_an_adapter() -> None:
    keys = _compose_keys()
    for key, adapter in REGISTRY.items():
        assert keys.get(key) == adapter.compose, f"{key}: no deploy/systems/{adapter.compose}/compose.yaml"
    for key in keys:
        assert key in REGISTRY or key in BUILT_IN, f"{key} has a container entry but no adapter"
    assert set(REGISTRY) <= set(SYSTEMS)
    assert {"ai_memory", "ai_memory_llm", "graphiti", "hindsight", "memobase", "supermemory", "memos"} <= set(
        REGISTRY
    )


def test_every_compose_file_pins_its_images() -> None:
    for path in (DEPLOY / "systems").glob("*/compose.yaml"):
        for image in re.findall(r"image:\s*(\S+)", path.read_text()):
            if image.startswith("${BENCH_HARNESS_IMAGE"):
                continue
            assert ":" in image and not image.endswith((":latest", ":main")), f"{path}: {image} is not pinned"


@pytest.mark.parametrize("key", sorted(REGISTRY))
def test_each_adapter_writes_every_session_and_reads_one_customer(key, cases) -> None:
    adapter = (
        REGISTRY[key](url="http://system.test", now=NOW)
        if key != "supermemory"
        else Supermemory(url="http://system.test")
    )
    for case in cases[::40]:
        ids = Identities.for_case(case, "t1")
        calls = adapter.seed_calls(case, ids)
        assert calls and all(isinstance(c, Call) and c.path.startswith("/") for c in calls)
        body = json.dumps([c.json for c in calls], ensure_ascii=False)
        # every turn of the history reaches the system, and no other customer's id does
        for session in case.sessions:
            for turn in session.turns:
                assert json.dumps(turn.text[:1500], ensure_ascii=False)[1:-1] in body
        other = Identities.for_case(case, "t2")
        assert adapter.store(other) not in body
        read = adapter.read_call(case, ids, case.probe.question)
        assert adapter.store(ids) in json.dumps(read.json or {}) + read.path + json.dumps(read.params or {})
        write = adapter.exchange_call(case, ids, "conv-1", "my order is 123456", "noted")
        assert "123456" in json.dumps(write.json, ensure_ascii=False)


def test_ai_memory_replays_sessions_at_the_hook_cadence(cases) -> None:
    case = next(c for c in cases if any(s.record for s in c.sessions))
    ids = Identities.for_case(case, "t1")
    session = next(s for s in case.sessions if s.turns)
    items = session_items(case, ids, "p1", session, NOW)
    events = [re.search(r"event=([a-z-]+)", i["url"]).group(1) for i in items]
    assert events[0] == "session-start" and events[-1] == "session-end"
    assert events.count("stop") == sum(t.role == "agent" for t in session.turns)
    assert all("workspace=niadra-bench" in i["url"] and "project=p1" in i["url"] for i in items)
    stop = next(i for i in items if "event=stop" in i["url"])
    assert "capture_assistant=1" in stop["url"] and stop["body"]["_ai_memory_assistant"]["version"] == 1
    prompt = next(i for i in items if "user-prompt-submit" in i["url"])["body"]["prompt"]
    assert prompt.startswith("[session date: 2026/")
    assert len(cap_excerpt("é" * 3000).encode()) <= 2000


def test_ai_memory_sends_again_what_the_server_skipped() -> None:
    adapter = AiMemory(url="http://ai.test")
    call = Call("POST", "/hook/batch", json=[{"i": 0}, {"i": 1}, {"i": 2}])
    request = httpx.Request("POST", "http://ai.test/hook/batch")
    rest = adapter.retry_of(call, httpx.Response(200, json={"accepted_indices": [0, 2]}, request=request))
    assert rest is not None and rest.json == [{"i": 1}]
    assert adapter.retry_of(call, httpx.Response(200, json={"accepted": 3}, request=request)) is None


def test_the_read_answers_become_the_lines_the_agent_receives() -> None:
    def answer(payload) -> httpx.Response:
        return httpx.Response(200, json=payload, request=httpx.Request("POST", "http://x"))

    tool = {
        "result": {
            "content": [{"type": "text", "text": json.dumps({"hits": [{"title": "t", "snippet": "s"}]})}]
        }
    }
    assert AiMemory(url="http://x").memories(answer(tool)) == ["t: s"]
    assert (
        fact_line({"fact": "order 1 shipped", "valid_at": "2026-09-01"})
        == "order 1 shipped (since 2026-09-01)"
    )
    facts = {"facts": [{"fact": "a", "valid_at": None, "invalid_at": None}]}
    assert Graphiti(url="http://x").memories(answer(facts)) == ["a"]
    recall = {"results": [{"id": "m1", "text": "b", "occurred_start": "2026-09-02"}]}
    assert Hindsight(url="http://x").memories(answer(recall)) == ["b (2026-09-02)"]
    context = {"data": {"context": "# Memory\n\n- basic_info: Ana"}}
    memobase = Memobase(url="http://x")
    assert memobase.render(memobase.memories(answer(context))) == "# Memory\n- basic_info: Ana"
    profile = {
        "profile": {"static": ["lives in Recife"], "dynamic": []},
        "searchResults": {"results": [{"memory": "c"}]},
    }
    lines = Supermemory(url="http://x").memories(answer(profile))
    assert lines == ["## Profile (static)", "- lives in Recife", "## Related memories", "- c"]
    nested = {"data": {"text_mem": [{"cube_id": "u", "memories": [{"id": "1", "memory": "d"}]}]}}
    assert MemOS(url="http://x").memories(answer(nested)) == ["d"]
    assert found_memories({"a": [{"memory": "e", "id": "2"}]}) == [{"memory": "e", "id": "2"}]


def test_hindsight_retains_a_conversation_as_one_dated_item(cases) -> None:
    case = cases[0]
    ids = Identities.for_case(case, "t1")
    session = next(s for s in case.sessions if s.turns)
    item = retain_item(case, ids, session, NOW, "bank")
    assert item["document_id"] == f"bank-{session.id}" and item["timestamp"].startswith("2026-")
    assert item["content"].count("\n") == len(session.turns) - 1


class FakeAiMemory:
    """ai-memory's documented routes, as far as the harness calls them: hooks, MCP tools, admin status."""

    def __init__(self) -> None:
        self.observations: dict[str, list[str]] = {}
        self.sessions: dict[str, set[str]] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        body = json.loads(await read_body(receive) or b"null")
        if scope["path"] == "/hook/batch":
            for item in body:
                query = dict(p.split("=", 1) for p in item["url"].split("?", 1)[1].split("&"))
                project = query["project"]
                self.sessions.setdefault(project, set()).add(query["session_id"])
                text = item["body"].get("prompt") or (item["body"].get("_ai_memory_assistant") or {}).get(
                    "excerpt"
                )
                if text:
                    self.observations.setdefault(project, []).append(text)
            await respond_json(send, 200, {"accepted": len(body), "accepted_indices": list(range(len(body)))})
            return
        if scope["path"] == "/admin/status":
            await respond_json(send, 200, {"write_queue": [0, 1024], "providers": {"llm": {}}})
            return
        tool, args = body["params"]["name"], body["params"]["arguments"]
        project = args.get("project", "")
        if tool == "memory_query":
            words = set(re.findall(r"\w+", args["query"].lower()))
            hits = [
                {"title": o[:40], "snippet": o, "path": f"sessions/{i}.md"}
                for i, o in enumerate(self.observations.get(project, []))
                if words & set(re.findall(r"\w+", o.lower()))
            ][: args.get("limit", 10)]
            result = {"hits": hits}
        elif tool == "memory_briefing":
            count = len(self.sessions.get(project, ()))
            result = {"counts": {"sessions": count, "pages_latest": count}}
        else:
            result = {"path": args.get("path"), "body": "page"}
        await respond_json(
            send,
            200,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
            },
        )


async def test_a_dry_run_measures_an_added_system_like_the_others(config, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NIADRA_API_KEY", MOCK_KEY)
    monkeypatch.delenv("NIADRA_BOOTSTRAP", raising=False)
    monkeypatch.delenv("MEM0_METER_URL", raising=False)
    mock = MockApp()
    fake = FakeAiMemory()
    options = Options(
        systems={"niadra", "ai_memory"},
        metrics={
            "latency",
            "accuracy",
            "tokens",
            "privacy",
            "cost",
            "resilience",
            "history",
            "ingest",
            "freshness",
        },
        repetitions=1,
        dry_run=True,
        limit=14,
        quick=True,
        output=tmp_path,
        dataset="v2",
        niadra_transport=lambda: httpx.ASGITransport(app=mock.asgi),
        niadra_base_url="http://niadra-mock",
        system_transports={"ai_memory": httpx.ASGITransport(app=fake)},
        extra={"tokenizer": word_tokenizer},
    )
    out = await Run(config, load_cases("v2"), options).execute()
    summary = json.loads((out / "summary.json").read_text())
    metrics = summary["metrics"]
    assert summary["versions"]["ai_memory"] == AiMemory.version
    accuracy = {r["system"]: r for r in metrics["accuracy"]["results"]}
    assert accuracy["ai_memory"]["scenario"] == "known_id" and accuracy["ai_memory"]["retrieve_errors"] == 0
    latency = {(r["system"], r["path"]) for r in metrics["latency"]["results"]}
    assert ("ai_memory", "host") in latency and ("niadra", "edge") in latency
    history = {(r["system"], r["operation"]) for r in metrics["history"]["results"]}
    assert {("ai_memory", "search"), ("ai_memory", "open")} <= history
    ingest = {r["system"]: r for r in metrics["ingest"]["results"]}
    assert ingest["ai_memory"]["operation"] == "write" and ingest["ai_memory"]["errors"] == 0
    fresh = {r["system"]: r for r in metrics["freshness"]["results"]}
    assert fresh["ai_memory"]["timeouts"] == 0
    faults = {(r["system"], r["fault"]) for r in metrics["resilience"]["results"]}
    assert {("ai_memory", "delay_2000ms"), ("ai_memory", "http_503")} <= faults
    cost = {r["system"]: r for r in metrics["cost"]["results"]}
    assert cost["ai_memory"]["memory_usd_per_1000"]["median"] == 0.0
    privacy = {r["system"]: r for r in metrics["privacy"]["results"]}
    assert (
        privacy["ai_memory"]["verification"] == "none"
        and privacy["niadra"]["verification"] == "per conversation"
    )
    rep = json.loads((out / "rep-1.json").read_text())
    assert rep["settle"]["ai_memory:known_id"]["settled"] is True


class TinySystem(HttpSystem):
    """An adapter written in a test: what adding a system takes."""

    system = "tiny"
    title = "Tiny"
    compose = "tiny"
    url_env = "TINY_URL"
    default_url = "http://tiny"
    version = "1"

    def seed_calls(self, case, ids):
        return [
            Call("POST", "/write", json={"who": self.customer(ids), "text": t.text})
            for s in case.sessions
            for t in s.turns
        ]

    def read_call(self, case, ids, question):
        return Call("POST", "/read", json={"who": self.customer(ids), "q": question})

    def memories(self, response):
        return response.json()["lines"]

    def exchange_call(self, case, ids, conversation, customer, agent):
        return Call("POST", "/write", json={"who": self.customer(ids), "text": customer})


async def test_an_adapter_needs_only_its_calls(cases) -> None:
    store: dict[str, list[str]] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/write":
            store.setdefault(body["who"], []).append(body["text"])
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"lines": store.get(body["who"], [])[-3:]})

    tiny = TinySystem(transport=httpx.MockTransport(answer))
    await tiny.start()
    case = cases[0]
    ids = Identities.for_case(case, "t1")
    await tiny.seed(case, ids)
    got = await tiny.retrieve(case, ids)
    assert got.error is None and got.text.startswith("- ") and got.meta["results"] == 3
    assert await tiny.freshness_trial(case, ids, 0.01, 1.0) is not None
    assert tiny.seed_report()["writes"] == sum(len(s.turns) for s in case.sessions) + 1
    await tiny.close()


async def test_runs_of_different_systems_combine_into_one_summary(config, tmp_path, monkeypatch) -> None:
    from niadra_bench.combine import combine

    monkeypatch.setenv("NIADRA_API_KEY", MOCK_KEY)
    monkeypatch.delenv("NIADRA_BOOTSTRAP", raising=False)
    monkeypatch.delenv("MEM0_METER_URL", raising=False)
    cases = load_cases("v2")
    outs = []
    for systems in ({"niadra"}, {"ai_memory"}):
        mock = MockApp()
        options = Options(
            systems=systems,
            metrics={"latency", "accuracy", "tokens", "privacy", "cost"},
            repetitions=1,
            dry_run=True,
            limit=8,
            quick=True,
            output=tmp_path / "runs",
            dataset="v2",
            niadra_transport=lambda mock=mock: httpx.ASGITransport(app=mock.asgi),
            niadra_base_url="http://niadra-mock",
            system_transports={"ai_memory": httpx.ASGITransport(app=FakeAiMemory())},
            extra={"tokenizer": word_tokenizer},
        )
        outs.append(await Run(config, cases, options).execute())
    combined = combine(outs, tmp_path / "combined", {c.id: c for c in cases})
    summary = json.loads((combined / "summary.json").read_text())
    systems = [r["system"] for r in summary["metrics"]["accuracy"]["results"]]
    assert sorted(systems) == ["ai_memory", "full_history", "niadra", "no_memory"]
    cost = [(r["system"], r["variant"]) for r in summary["metrics"]["cost"]["results"]]
    assert cost.count(("niadra", "price_low")) == 1 and ("ai_memory", "models_only") in cost
    assert [s["systems"] for s in summary["combined_from"]] == [["niadra"], ["ai_memory"]]
    rows = (combined / "cases-rep1.jsonl").read_text().splitlines()
    assert sum('"system": "no_memory"' in r for r in rows) == 8


def test_the_production_caps_apply_to_the_region_only(config, cases, monkeypatch) -> None:
    def run(**options) -> Run:
        return Run(config, cases[:2], Options(systems={"niadra"}, metrics=set(), repetitions=1, **options))

    monkeypatch.setenv("BENCH_ENVIRONMENT", "region")
    assert run(dry_run=True).against_production  # bench ab's context agent still reaches the cell
    assert not run(
        niadra_transport=lambda: httpx.MockTransport(lambda r: httpx.Response(200))
    ).against_production
    assert not run(niadra_bootstrap=Path("cell/bootstrap.json")).against_production
    assert run()._niadra_rates([10, 25], [10]) == [10]
    monkeypatch.setenv("BENCH_ENVIRONMENT", "local")
    assert not run().against_production and run()._niadra_rates([10, 25], [10]) == [10, 25]
