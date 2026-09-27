"""The systems added through adapters: the registry, the container entries, each adapter's calls, and a
dry run with one of them against an in-process fake of its documented routes."""

import inspect
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
from niadra_bench.systems.base import Call, HttpSystem
from niadra_bench.systems.cognee import Cognee, answer_lines
from niadra_bench.systems.graphiti import Graphiti, fact_line
from niadra_bench.systems.hindsight import Hindsight, retain_item
from niadra_bench.systems.honcho import AGENT_PEER, Honcho, HonchoDialectic, session_messages
from niadra_bench.systems.langmem import LangMem
from niadra_bench.systems.memobase import Memobase
from niadra_bench.systems.memos import MemOS, found_memories
from niadra_bench.systems.redis_agent_memory import RedisAgentMemory, working_memory
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
    first_row = {"graphiti", "hindsight", "hindsight_reflect", "memobase", "supermemory", "memos"}
    second_row = {"redis_agent_memory", "honcho", "honcho_dialectic", "langmem", "cognee"}
    assert first_row | second_row <= set(REGISTRY)


def test_every_compose_file_pins_its_images() -> None:
    for path in (DEPLOY / "systems").glob("*/compose.yaml"):
        for image in re.findall(r"image:\s*(\S+)", path.read_text()):
            if image.startswith("${BENCH_HARNESS_IMAGE"):
                continue
            assert ":" in image and not image.endswith((":latest", ":main")), f"{path}: {image} is not pinned"


@pytest.mark.parametrize("key", sorted(REGISTRY))
def test_each_adapter_writes_every_session_and_reads_one_customer(key, cases) -> None:
    cls = REGISTRY[key]
    dated = "now" in inspect.signature(cls.__init__).parameters
    adapter = cls(url="http://system.test", **({"now": NOW} if dated else {}))
    for case in cases[::40]:
        ids = Identities.for_case(case, "t1")
        calls = adapter.seed_calls(case, ids)
        assert calls and all(isinstance(c, Call) and c.path.startswith("/") for c in calls)
        body = json.dumps([c.body for c in calls], ensure_ascii=False)
        # every turn of the history reaches the system, and no other customer's id does
        for session in case.sessions:
            for turn in session.turns:
                assert json.dumps(turn.text[:1500], ensure_ascii=False)[1:-1] in body
        other = Identities.for_case(case, "t2")
        assert adapter.store(other) not in body
        read = adapter.read_call(case, ids, case.probe.question)
        assert adapter.store(ids) in json.dumps(read.json or {}) + read.path + json.dumps(read.params or {})
        write = adapter.exchange_call(case, ids, "conv-1", "my order is 123456", "noted")
        assert "123456" in json.dumps(write.body, ensure_ascii=False)


def test_the_read_answers_become_the_lines_the_agent_receives() -> None:
    def answer(payload) -> httpx.Response:
        return httpx.Response(200, json=payload, request=httpx.Request("POST", "http://x"))

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


def test_the_second_row_reads_become_the_lines_the_agent_receives() -> None:
    def answer(payload) -> httpx.Response:
        return httpx.Response(200, json=payload, request=httpx.Request("POST", "http://x"))

    found = {"memories": [{"id": "m1", "text": "order 1 shipped", "event_date": "2026-09-01T00:00:00Z"}]}
    redis = RedisAgentMemory(url="http://x")
    assert redis.memories(answer(found)) == ["order 1 shipped (event date: 2026-09-01T00:00:00Z)"]
    assert redis.open_call(None, None, answer(found)).path == "/v1/long-term-memory/m1"
    context = {"peer_card": ["Name: Ana"], "representation": "Ana moved to Recife.\n\nShe asked."}
    honcho = Honcho(url="http://x")
    lines = honcho.memories(answer(context))
    assert lines == ["## Peer card", "Name: Ana", "## Representation", "Ana moved to Recife.", "She asked."]
    assert honcho.render(lines).splitlines()[1] == "- Name: Ana"
    assert HonchoDialectic(url="http://x").memories(answer({"content": "Protocol 123.\nAnything else?"})) == [
        "Protocol 123.",
        "Anything else?",
    ]
    stored = {"memories": [{"key": "k1", "content": "User's order is 991"}]}
    langmem = LangMem(url="http://x")
    assert langmem.memories(answer(stored)) == ["User's order is 991"]
    results = [{"search_result": ["The protocol is 4411."], "dataset_id": None, "dataset_name": "d"}]
    assert Cognee(url="http://x").memories(answer(results)) == ["The protocol is 4411."]
    assert answer_lines(["a\nb", {"text": "c"}]) == ["a", "b", "c"]


def test_the_second_row_writes_as_each_system_documents_it(cases) -> None:
    case = next(c for c in cases if any(s.record for s in c.sessions))
    ids = Identities.for_case(case, "t1")
    session = next(s for s in case.sessions if s.turns)
    memory = working_memory(case, ids, session, NOW, "u1")
    assert memory["user_id"] == "u1" and len(memory["messages"]) == len(session.turns)
    assert all(m["created_at"].startswith("2026-") for m in memory["messages"])
    record = next(s for s in case.sessions if s.record)
    assert working_memory(case, ids, record, NOW, "u1")["messages"][0]["role"] == "system"
    messages = session_messages(case, ids, session, NOW, "peer-1")
    agents = sum(t.role == "agent" for t in session.turns)
    assert sum(m["peer_id"] == AGENT_PEER for m in messages) == agents
    honcho = Honcho(url="http://x", now=NOW)
    ensure = honcho.ensure_calls(case, ids)
    assert [c.json.get("configuration") for c in ensure[2:]] == [{"observe_me": False}] * 2
    assert HonchoDialectic.workspace != Honcho.workspace
    seeded = Cognee(url="http://x").seed_calls(case, ids)
    assert seeded[-1].path == "/api/v1/cognify" and all(c.form for c in seeded[:-1])
    live = Cognee(url="http://x").exchange_call(case, ids, "conv", "hi", None)
    assert live.path == "/api/v1/remember" and live.form and live.form["run_in_background"] == "true"
    queued = LangMem(url="http://x").seed_calls(case, ids)
    assert len(queued) == len(case.sessions) and queued[0].json["messages"]


def test_hindsight_retains_a_conversation_as_one_dated_item(cases) -> None:
    case = cases[0]
    ids = Identities.for_case(case, "t1")
    session = next(s for s in case.sessions if s.turns)
    item = retain_item(case, ids, session, NOW, "bank")
    assert item["document_id"] == f"bank-{session.id}" and item["timestamp"].startswith("2026-")
    assert item["content"].count("\n") == len(session.turns) - 1


class FakeHindsight:
    """Hindsight's documented routes, as far as the harness calls them (retain, recall, one memory, the
    bank's operations), and its model gateway's counters, which grow with every retain."""

    def __init__(self) -> None:
        self.facts: dict[str, list[dict[str, str]]] = {}
        self.retains = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        path, method = scope["path"], scope["method"]
        body = json.loads(await read_body(receive) or b"null")
        if path == "/health":
            await respond_json(send, 200, {"status": "healthy"})
            return
        if path == "/_meter":
            calls = self.retains
            usage = {"calls": calls, "prompt_tokens": 3000 * calls, "completion_tokens": 400 * calls}
            usage |= {"cached_tokens": 1200 * calls, "reasoning_tokens": 150 * calls}
            await respond_json(send, 200, {"models": {"openai/gpt-6-luna": usage}})
            return
        bank = path.split("/")[4]
        facts = self.facts.setdefault(bank, [])
        if path.endswith("/memories") and method == "POST":
            self.retains += 1
            for item in body["items"]:
                for line in item["content"].splitlines():
                    facts.append({"id": f"m{len(facts)}", "text": line, "occurred_start": item["timestamp"]})
            await respond_json(send, 200, {"success": True})
            return
        if path.endswith("/memories/recall"):
            words = set(re.findall(r"\w+", body["query"].lower()))
            found = [f for f in facts if words & set(re.findall(r"\w+", f["text"].lower()))][:10]
            await respond_json(send, 200, {"results": found})
            return
        if path.endswith("/operations"):
            await respond_json(send, 200, {"total": 0, "items": []})
            return
        await respond_json(send, 200, {"id": path.rsplit("/", 1)[-1], "text": "fact"})


async def test_a_dry_run_measures_an_added_system_like_the_others(config, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NIADRA_API_KEY", MOCK_KEY)
    monkeypatch.delenv("NIADRA_BOOTSTRAP", raising=False)
    monkeypatch.delenv("MEM0_METER_URL", raising=False)
    mock = MockApp()
    fake = FakeHindsight()
    options = Options(
        systems={"niadra", "hindsight"},
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
        system_transports={"hindsight": httpx.ASGITransport(app=fake)},
        extra={"tokenizer": word_tokenizer},
    )
    out = await Run(config, load_cases("v2"), options).execute()
    summary = json.loads((out / "summary.json").read_text())
    metrics = summary["metrics"]
    assert summary["versions"]["hindsight"] == Hindsight.version
    accuracy = {r["system"]: r for r in metrics["accuracy"]["results"]}
    assert accuracy["hindsight"]["scenario"] == "known_id" and accuracy["hindsight"]["retrieve_errors"] == 0
    latency = {(r["system"], r["path"]) for r in metrics["latency"]["results"]}
    assert ("hindsight", "host") in latency and ("niadra", "edge") in latency
    history = {(r["system"], r["operation"]) for r in metrics["history"]["results"]}
    assert {("hindsight", "search"), ("hindsight", "open")} <= history
    ingest = {r["system"]: r for r in metrics["ingest"]["results"]}
    assert ingest["hindsight"]["operation"] == "write" and ingest["hindsight"]["errors"] == 0
    fresh = {r["system"]: r for r in metrics["freshness"]["results"]}
    assert fresh["hindsight"]["timeouts"] == 0
    faults = {(r["system"], r["fault"]) for r in metrics["resilience"]["results"]}
    assert {("hindsight", "delay_2000ms"), ("hindsight", "http_503")} <= faults
    cost = {r["system"]: r for r in metrics["cost"]["results"]}
    # Luna's prices, cache reads at their own: every retain spent 1,800 fresh input tokens, 1,200 read from
    # the cache and 400 of output.
    assert cost["hindsight"]["memory_usd_per_1000"]["median"] > 0.0
    privacy = {r["system"]: r for r in metrics["privacy"]["results"]}
    assert (
        privacy["hindsight"]["verification"] == "none"
        and privacy["niadra"]["verification"] == "per conversation"
    )
    rep = json.loads((out / "rep-1.json").read_text())
    assert rep["settle"]["hindsight:known_id"]["settled"] is True


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


def test_a_campaign_cap_shortens_a_settle_but_never_lengthens_it() -> None:
    graphiti = REGISTRY["graphiti"]
    assert graphiti(settle_timeout_s=1200).settle_timeout_s == graphiti.min_settle_timeout_s
    assert graphiti(settle_timeout_s=1200, max_settle_s=6 * 3600).settle_timeout_s == 6 * 3600
    assert TinySystem(settle_timeout_s=1200, max_settle_s=3 * 3600).settle_timeout_s == 1200


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
    for systems in ({"niadra"}, {"hindsight"}):
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
            system_transports={"hindsight": httpx.ASGITransport(app=FakeHindsight())},
            extra={"tokenizer": word_tokenizer},
        )
        outs.append(await Run(config, cases, options).execute())
    combined = combine(outs, tmp_path / "combined", {c.id: c for c in cases})
    summary = json.loads((combined / "summary.json").read_text())
    systems = [r["system"] for r in summary["metrics"]["accuracy"]["results"]]
    assert sorted(systems) == ["full_history", "hindsight", "niadra", "no_memory"]
    cost = [(r["system"], r["variant"]) for r in summary["metrics"]["cost"]["results"]]
    assert cost.count(("niadra", "price_low")) == 1 and ("hindsight", "models_only") in cost
    assert [s["systems"] for s in summary["combined_from"]] == [["niadra"], ["hindsight"]]
    rows = (combined / "cases-rep1.jsonl").read_text().splitlines()
    assert sum('"system": "no_memory"' in r for r in rows) == 8


async def test_runs_with_fewer_repetitions_and_cases_combine_after_the_first(
    config, tmp_path, monkeypatch
) -> None:
    from niadra_bench.combine import combine

    monkeypatch.setenv("NIADRA_API_KEY", MOCK_KEY)
    monkeypatch.delenv("NIADRA_BOOTSTRAP", raising=False)
    monkeypatch.delenv("MEM0_METER_URL", raising=False)
    cases = load_cases("v2")
    outs = []
    for systems, repetitions, limit, references in (({"niadra"}, 2, 8, True), ({"hindsight"}, 1, 4, False)):
        mock = MockApp()
        options = Options(
            systems=systems,
            metrics={"latency", "accuracy", "tokens", "privacy", "cost"},
            repetitions=repetitions,
            dry_run=True,
            limit=limit,
            quick=True,
            output=tmp_path / "runs",
            dataset="v2",
            references=references,
            niadra_transport=lambda mock=mock: httpx.ASGITransport(app=mock.asgi),
            niadra_base_url="http://niadra-mock",
            system_transports={"hindsight": httpx.ASGITransport(app=FakeHindsight())},
            extra={"tokenizer": word_tokenizer},
        )
        outs.append(await Run(config, cases, options).execute())
    by_id = {c.id: c for c in cases}
    second = json.loads((outs[1] / "summary.json").read_text())
    assert second["config"]["references"] == "off"
    with pytest.raises(ValueError, match="no references"):
        combine(list(reversed(outs)), tmp_path / "wrong", by_id)
    combined = combine(outs, tmp_path / "combined", by_id)
    summary = json.loads((combined / "summary.json").read_text())
    sources = [(s["systems"], s["repetitions"], s["cases"]) for s in summary["combined_from"]]
    assert sources == [(["niadra"], 2, 8), (["hindsight"], 1, 4)]
    assert summary["config"]["repetitions"] == 2
    assert summary["config"]["quick"] is True and second["config"]["limit"] == 4
    line = next(r for r in summary["metrics"]["accuracy"]["results"] if r["system"] == "hindsight")
    assert line["cases"]["runs"][1] is None
    assert '"system": "hindsight"' not in (combined / "cases-rep2.jsonl").read_text()
    for n in (1, 2):
        rows = (combined / f"cases-rep{n}.jsonl").read_text().splitlines()
        assert sum('"system": "no_memory"' in r for r in rows) == 8
    # Each row keeps the block the agent received (the references' is the case itself, never kept).
    first = [json.loads(r) for r in (combined / "cases-rep1.jsonl").read_text().splitlines()]
    assert all(r["context"] for r in first if r["system"] == "niadra" and r["purpose"] == "answer")
    assert all(r["context"] is None for r in first if r["system"] in ("no_memory", "full_history"))


async def test_separate_runs_of_one_system_stack_into_repetitions(config, tmp_path, monkeypatch) -> None:
    from niadra_bench.combine import combine
    from niadra_bench.stack import stack

    monkeypatch.setenv("NIADRA_API_KEY", MOCK_KEY)
    monkeypatch.delenv("NIADRA_BOOTSTRAP", raising=False)
    monkeypatch.delenv("MEM0_METER_URL", raising=False)
    cases = load_cases("v2")

    async def one(systems: set[str], repetitions: int, references: bool) -> Path:
        mock = MockApp()
        options = Options(
            systems=systems,
            metrics={"latency", "accuracy", "tokens", "privacy", "cost"},
            repetitions=repetitions,
            dry_run=True,
            limit=8,
            quick=True,
            output=tmp_path / "runs",
            dataset="v2",
            references=references,
            niadra_transport=lambda mock=mock: httpx.ASGITransport(app=mock.asgi),
            niadra_base_url="http://niadra-mock",
            system_transports={"hindsight": httpx.ASGITransport(app=FakeHindsight())},
            extra={"tokenizer": word_tokenizer},
        )
        return await Run(config, cases, options).execute()

    first, second = await one({"niadra"}, 1, True), await one({"niadra"}, 1, True)
    other = await one({"hindsight"}, 2, False)
    with pytest.raises(ValueError, match="at least two"):
        stack([first], tmp_path / "stacked")
    with pytest.raises(ValueError, match="another config references"):
        stack([first, other], tmp_path / "stacked")
    stacked = stack([first, second], tmp_path / "stacked")
    summary = json.loads((stacked / "summary.json").read_text())
    runs = [json.loads((d / "summary.json").read_text())["run_id"] for d in (first, second)]
    assert summary["config"]["repetitions"] == 2
    assert [(s["run_id"], s["repetitions"]) for s in summary["stacked_from"]] == [(runs[0], 1), (runs[1], 1)]
    assert json.loads((stacked / "rep-2.json").read_text())["stacked_from"] == {
        "run_id": runs[1],
        "repetition": 1,
    }
    second_rows = [json.loads(r) for r in (stacked / "cases-rep2.jsonl").read_text().splitlines()]
    assert second_rows and all(r["repetition"] == 2 for r in second_rows)
    assert second_rows == [
        {**json.loads(r), "repetition": 2} for r in (second / "cases-rep1.jsonl").read_text().splitlines()
    ]
    line = next(r for r in summary["metrics"]["accuracy"]["results"] if r["system"] == "niadra")
    assert len(line["cases"]["runs"]) == 2
    # The stack is a run like any other: a combine takes it first, beside a run of two repetitions.
    combined = json.loads(
        (
            combine([stacked, other], tmp_path / "combined", {c.id: c for c in cases}) / "summary.json"
        ).read_text()
    )
    assert [(s["systems"], s["repetitions"]) for s in combined["combined_from"]] == [
        (["niadra"], 2),
        (["hindsight"], 2),
    ]


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
