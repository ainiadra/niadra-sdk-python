import base64
import struct

import httpx
import pytest

from niadra_bench.services.embed_proxy import EmbedProxy
from niadra_bench.services.fakes import FakeLlm, FakeModels, hashed_vector
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
