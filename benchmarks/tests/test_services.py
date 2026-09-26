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


async def test_meter_counts_tokens_per_model() -> None:
    meter = LlmMeter("http://llm/v1", transport=httpx.ASGITransport(app=FakeLlm()))
    async with _client(meter) as client:
        body = {"model": "google/gemini-2.5-flash-lite", "messages": [{"role": "user", "content": "x" * 400}]}
        assert (await client.post("/v1/chat/completions", json=body)).status_code == 200
        await client.post("/v1/chat/completions", json=body)
        counts = (await client.get("/_meter")).json()["models"]["google/gemini-2.5-flash-lite"]
        assert counts["calls"] == 2 and counts["prompt_tokens"] == 200 and counts["failed"] == 0


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
        force_model="google/gemini-2.5-flash-lite",
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
    assert set(counts) == {"google/gemini-2.5-flash-lite"}
    assert counts["google/gemini-2.5-flash-lite"]["prompt_tokens"] == 17
    chat, _responses, embeddings = seen
    assert json.loads(chat.content)["model"] == "google/gemini-2.5-flash-lite"
    assert chat.headers["authorization"] == "Bearer provider-key"
    assert str(embeddings.url) == "http://embed/v1/embeddings"
    assert embeddings.headers["authorization"] == "Bearer placeholder"  # the key never goes to the embedder


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
