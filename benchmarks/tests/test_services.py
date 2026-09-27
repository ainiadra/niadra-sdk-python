import base64
import json
import struct

import httpx
import pytest

from niadra_bench.net import PinnedTransport, niadra_routes
from niadra_bench.services.embed_proxy import EmbedProxy
from niadra_bench.services.fakes import FakeLlm, FakeModels, hashed_vector, minimal
from niadra_bench.services.fault_proxy import Fault, FaultProxy
from niadra_bench.services.llm_meter import LlmMeter


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc")


async def test_embed_proxy_serves_floats_and_base64() -> None:
    proxy = EmbedProxy("http://models", transport=httpx.ASGITransport(app=FakeModels()))
    async with _client(proxy) as client:
        floats = (await client.post("/v1/embeddings", json={"input": ["oi"], "model": "m"})).json()
        assert floats["data"][0]["embedding"] == hashed_vector("oi")
        b64 = (await client.post("/v1/embeddings", json={"input": "oi", "encoding_format": "base64"})).json()
        raw = base64.b64decode(b64["data"][0]["embedding"])
        assert list(struct.unpack(f"<{len(raw) // 4}f", raw)) == pytest.approx(hashed_vector("oi"), abs=1e-6)
        wrong = await client.post("/v1/embeddings", json={"input": "oi", "dimensions": 1536})
        assert wrong.status_code == 400
        assert (
            await client.post("/v1/embeddings", json={"input": ["oi"], "dimensions": 384})
        ).status_code == 200


async def test_embed_proxy_splits_what_the_embedder_refuses_in_one_request() -> None:
    models = FakeModels(max_texts=4, max_chars=50)
    transport = httpx.ASGITransport(app=models)
    texts = [f"memoria {i}" for i in range(10)] + ["x" * 80]
    async with _client(EmbedProxy("http://models", transport=transport)) as client:
        # The embedder's limits as they are: the whole list at once is refused.
        assert (await client.post("/v1/embeddings", json={"input": texts})).status_code == 502
    proxy = EmbedProxy("http://models", transport=transport, max_texts=4, max_chars=50)
    async with _client(proxy) as client:
        answer = await client.post("/v1/embeddings", json={"input": texts})
    assert answer.status_code == 200 and models.requests == 1 + 3
    vectors = [d["embedding"] for d in answer.json()["data"]]
    assert vectors == [hashed_vector(t) for t in texts[:10]] + [hashed_vector("x" * 50)]


async def test_meter_counts_tokens_per_model() -> None:
    meter = LlmMeter("http://llm/v1", transport=httpx.ASGITransport(app=FakeLlm()))
    async with _client(meter) as client:
        body = {"model": "openai/gpt-6-luna", "messages": [{"role": "user", "content": "x" * 400}]}
        assert (await client.post("/v1/chat/completions", json=body)).status_code == 200
        await client.post("/v1/chat/completions", json=body)
        counts = (await client.get("/_meter")).json()["models"]["openai/gpt-6-luna"]
        assert counts["calls"] == 2 and counts["prompt_tokens"] == 200 and counts["failed"] == 0


async def test_meter_counts_a_responses_call_with_a_null_error_as_answered() -> None:
    meter = LlmMeter("http://llm/v1", transport=httpx.ASGITransport(app=FakeLlm()))
    async with _client(meter) as client:
        body = {"model": "openai/gpt-6-luna", "input": "x" * 400}
        answer = await client.post("/v1/responses", json=body)
        assert answer.status_code == 200 and answer.json()["error"] is None
        counts = (await client.get("/_meter")).json()["models"]["openai/gpt-6-luna"]
        assert counts["calls"] == 1 and counts["failed"] == 0 and counts["prompt_tokens"] > 0


async def test_fake_llm_answers_mem0_extraction_in_its_json_shape() -> None:
    async with _client(FakeLlm()) as client:
        body = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "New messages:\nuser: my order is 4471\nassistant: noted"}
            ],
            "response_format": {"type": "json_object"},
        }
        content = (await client.post("/v1/chat/completions", json=body)).json()["choices"][0]["message"][
            "content"
        ]
        assert '"memory"' in content and "4471" in content


async def test_fault_proxy_injects_status_and_forwards_otherwise() -> None:
    upstream = httpx.ASGITransport(app=FakeModels())
    async with _client(FaultProxy("http://models", Fault(status=503), transport=upstream)) as client:
        assert (await client.post("/v1/embed", json={"texts": ["a"]})).status_code == 503
    async with _client(FaultProxy("http://models", Fault(delay_ms=10), transport=upstream)) as client:
        assert (await client.post("/v1/embed", json={"texts": ["a"]})).json()["dim"] == 384
    assert Fault(delay_ms=2000).name == "delay_2000ms" and Fault(status=503).name == "http_503"


async def test_the_gateway_sends_embeddings_to_the_embedder_pins_the_model_and_holds_the_key() -> None:
    seen: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        usage = {"input_tokens": 10, "output_tokens": 2} if "responses" in request.url.path else None
        return httpx.Response(200, json={"usage": usage or {"prompt_tokens": 7, "completion_tokens": 1}})

    meter = LlmMeter(
        "http://llm/v1",
        transport=httpx.MockTransport(provider),
        embed_upstream="http://embed/v1",
        force_model="openai/gpt-6-luna",
        api_key="provider-key",
    )
    async with _client(meter) as client:
        headers = {"authorization": "Bearer placeholder"}
        await client.post(
            "/v1/chat/completions", json={"model": "gpt-4.1-nano", "messages": []}, headers=headers
        )
        await client.post("/v1/responses", json={"model": "gpt-5.5", "input": "x"}, headers=headers)
        await client.post(
            "/v1/embeddings", json={"model": "text-embedding-3-small", "input": "x"}, headers=headers
        )
        counts = (await client.get("/_meter")).json()["models"]
    assert set(counts) == {"openai/gpt-6-luna"}
    assert counts["openai/gpt-6-luna"]["prompt_tokens"] == 17
    chat, _responses, embeddings = seen
    assert json.loads(chat.content)["model"] == "openai/gpt-6-luna"
    assert chat.headers["authorization"] == "Bearer provider-key"
    assert str(embeddings.url) == "http://embed/v1/embeddings"
    assert embeddings.headers["authorization"] == "Bearer placeholder"  # the key never goes to the embedder


async def test_the_gateway_asks_every_call_for_the_benchmarks_reasoning_effort() -> None:
    seen: list[dict] = []

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        usage = {
            "prompt_tokens": 2000,
            "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 1024, "cache_write_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 250},
        }
        choice = {"finish_reason": "length" if len(seen) == 3 else "stop", "message": {"content": "x"}}
        return httpx.Response(200, json={"choices": [choice], "usage": usage})

    meter = LlmMeter("http://llm/v1", transport=httpx.MockTransport(provider), reasoning_effort="low")
    async with _client(meter) as client:
        luna = {"model": "openai/gpt-6-luna", "messages": []}
        # Set the documented way (Mem0, Honcho), with none (Graphiti with a model name it does not know),
        # and with a token budget instead of an effort.
        await client.post("/v1/chat/completions", json={**luna, "reasoning_effort": "low"})
        await client.post("/v1/chat/completions", json=luna)
        await client.post("/v1/chat/completions", json={**luna, "reasoning": {"max_tokens": 900}})
        counts = (await client.get("/_meter")).json()["models"]["openai/gpt-6-luna"]
    assert [body["reasoning"] for body in seen] == [{"effort": "low"}] * 3
    assert all("reasoning_effort" not in body for body in seen)
    assert counts["reasoning_set"] == 2 and counts["truncated"] == 1
    assert counts["cached_tokens"] == 3 * 1024 and counts["reasoning_tokens"] == 3 * 250


async def test_the_agent_sends_luna_its_effort_and_seed_and_no_temperature() -> None:
    from niadra_bench import config as bench_config
    from niadra_bench.agent import ChatClient

    sent: list[dict] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": " yes "}}]})

    config = bench_config.load()
    chat = ChatClient(base_url="http://llm/v1", api_key="k", transport=httpx.MockTransport(provider))
    assert await chat.complete(config.agent, [{"role": "user", "content": "q"}]) == "yes"
    await chat.close()
    body = sent[0]
    assert body["model"] == config.models.llm == config.judge.model == "openai/gpt-6-luna"
    assert body["reasoning"] == {"effort": "low"} and body["seed"] == 20260924
    assert "temperature" not in body and body["max_tokens"] == 2000


def test_every_compose_default_is_the_benchmarks_model_and_effort() -> None:
    import re
    from pathlib import Path

    from niadra_bench import config as bench_config

    config = bench_config.load()
    systems = Path(__file__).resolve().parents[1] / "deploy" / "systems"
    models, efforts = set(), set()
    for path in systems.glob("*/compose.yaml"):
        text = path.read_text()
        models |= set(re.findall(r"\$\{BENCH_LLM_MODEL:-([^}]+)\}", text))
        efforts |= set(re.findall(r"\$\{BENCH_LLM_REASONING_EFFORT:-([^}]+)\}", text))
        assert "LLM_REASONING_EFFORT: ${BENCH_LLM_REASONING_EFFORT" in text, (
            f"{path}: its gateway sets no effort"
        )
    assert models == {config.models.llm} and efforts == {config.models.reasoning_effort}
    memobase = (systems / "memobase" / "config.yaml").read_text()
    assert set(re.findall(r"_llm_model: (\S+)", memobase)) == {config.models.llm}
    mem0 = json.loads(bench_config.CONFIG_DIR.joinpath("mem0.config.json").read_text())
    for llm in (mem0["llm"]["config"], mem0["reranker"]["config"]["llm"]["config"]):
        assert (llm["model"], llm["reasoning_effort"]) == (config.models.llm, config.models.reasoning_effort)


def test_the_fake_llm_answers_the_smallest_value_a_schema_accepts() -> None:
    schema = {
        "type": "object",
        "required": ["entities", "summary", "kind", "item"],
        "properties": {
            "entities": {"type": "array", "items": {"type": "string"}},
            "summary": {"anyOf": [{"type": "null"}, {"type": "string"}]},
            "kind": {"enum": ["a", "b"]},
            "item": {"$ref": "#/$defs/Item"},
        },
        "$defs": {"Item": {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}},
    }
    assert minimal(schema) == {"entities": [], "summary": "", "kind": "a", "item": {"n": 0}}


async def test_the_fake_llm_serves_structured_output_on_both_apis() -> None:
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    async with _client(FakeLlm()) as client:
        chat = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [],
                "response_format": {"type": "json_schema", "json_schema": {"schema": schema}},
            },
        )
        assert json.loads(chat.json()["choices"][0]["message"]["content"]) == {"ok": False}
        response = await client.post(
            "/v1/responses",
            json={"input": "x", "text": {"format": {"type": "json_schema", "schema": schema}}},
        )
        assert json.loads(response.json()["output"][0]["content"][0]["text"]) == {"ok": False}


async def test_the_vpc_path_connects_to_the_private_address_with_the_public_name() -> None:
    seen: list[httpx.Request] = []
    inner = httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200))
    async with httpx.AsyncClient(transport=PinnedTransport("10.40.1.10", inner)) as client:
        await client.get("https://space.us-east-2.api.niadra.com/v1/context")
    [request] = seen
    assert request.url.host == "10.40.1.10" and request.headers["host"] == "space.us-east-2.api.niadra.com"
    assert request.extensions["sni_hostname"] == "space.us-east-2.api.niadra.com"
    routes = niadra_routes("https://edge", env={"NIADRA_VPC_ADDRESS": "10.40.1.10"})
    assert [r.path for r in routes] == ["edge", "vpc"] and routes[1].base == "https://edge"
    assert [r.path for r in niadra_routes("https://edge", env={})] == ["edge"]


async def test_the_fake_llm_ends_an_agent_loop_with_a_tool_call() -> None:
    tools = [
        {"type": "function", "function": {"name": "recall", "parameters": {"type": "object"}}},
        {
            "type": "function",
            "function": {
                "name": "done",
                "parameters": {
                    "type": "object",
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                },
            },
        },
    ]
    async with _client(FakeLlm()) as client:
        answer = await client.post("/v1/chat/completions", json={"messages": [], "tools": tools})
    [call] = answer.json()["choices"][0]["message"]["tool_calls"]
    assert call["function"]["name"] == "done" and json.loads(call["function"]["arguments"]) == {"answer": ""}
