"""The local cell's cache of model answers (deploy/local/model_cache.py): what hits, what is kept, what it
cost. The module sits beside cell_server.py, outside the package, so it is loaded from its path."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from niadra_bench import config as bench_config

_PATH = bench_config.ROOT / "deploy" / "local" / "model_cache.py"
_SPEC = importlib.util.spec_from_file_location("model_cache", _PATH)
assert _SPEC and _SPEC.loader
model_cache = importlib.util.module_from_spec(_SPEC)
sys.modules["model_cache"] = model_cache
_SPEC.loader.exec_module(model_cache)

LUNA = {
    "model": "openai/gpt-6-luna",
    "messages": [
        {"role": "system", "content": "extract"},
        {"role": "user", "content": "[e1] 10:00 customer: oi"},
    ],
    "reasoning": {"effort": "low"},
}
ANSWER = {
    "choices": [{"message": {"content": "{}"}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0002},
}


class Provider:
    def __init__(self, status: int = 200, body: dict | None = None, delay: float = 0.0) -> None:
        self.calls = 0
        self.status = status
        self.body = ANSWER if body is None else body
        self.delay = delay

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return httpx.Response(self.status, json=self.body)


def _client(provider: Provider, directory: Path | None, version: str = "x1.p6"):
    ledger = model_cache.Ledger()
    transport = model_cache.CachingTransport(
        httpx.MockTransport(provider), directory, ledger, default_version=version
    )
    return httpx.AsyncClient(transport=transport), ledger


async def _post(client: httpx.AsyncClient, body: dict) -> httpx.Response:
    return await client.post("https://openrouter.ai/api/v1/chat/completions", json=body)


async def test_the_second_identical_request_is_read_from_disk_and_costs_nothing(tmp_path) -> None:
    provider = Provider()
    client, ledger = _client(provider, tmp_path)
    first = await _post(client, LUNA)
    second = await _post(client, LUNA)
    assert provider.calls == 1
    assert first.json() == second.json() == ANSWER
    tally = ledger.snapshot()["models"]["openai/gpt-6-luna"]
    assert (tally["calls"], tally["hits"], tally["misses"], tally["failed"]) == (2, 1, 1, 0)
    assert tally["spend_usd"] == tally["saved_usd"] == 0.0002
    assert (tally["input_tokens"], tally["output_tokens"]) == (100, 20)
    # A rerun is a new process: its ledger starts at zero and reads the same file.
    rerun, fresh = _client(provider, tmp_path)
    await _post(rerun, LUNA)
    assert provider.calls == 1 and fresh.snapshot()["spend_usd"] == 0


def test_the_key_is_the_model_the_prompt_version_and_the_body_not_its_key_order() -> None:
    key = model_cache.request_key
    reordered = dict(reversed(list(LUNA.items())))
    assert key("m", "x1.p6", LUNA) == key("m", "x1.p6", reordered)
    assert key("m", "x1.p6", LUNA) != key("m", "x1.p7", LUNA)
    assert key("m", "x1.p6", LUNA) != key("n", "x1.p6", LUNA)
    changed = {
        **LUNA,
        "messages": [*LUNA["messages"][:1], {"role": "user", "content": "[e1] 10:01 customer: oi"}],
    }
    assert key("m", "x1.p6", LUNA) != key("m", "x1.p6", changed)


async def test_a_new_prompt_version_asks_the_provider_again(tmp_path) -> None:
    provider = Provider()
    client, _ = _client(provider, tmp_path, "x1.p6")
    await _post(client, LUNA)
    other, _ = _client(provider, tmp_path, "x1.p7")
    await _post(other, LUNA)
    assert provider.calls == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, {"error": {"message": "upstream"}}), (200, {"error": {"message": "inside a 200"}}), (200, {})],
)
async def test_a_failed_answer_is_passed_on_and_never_kept(tmp_path, status, body) -> None:
    provider = Provider(status, body)
    client, ledger = _client(provider, tmp_path)
    response = await _post(client, LUNA)
    assert response.status_code == status
    await _post(client, LUNA)
    assert provider.calls == 2 and not list(tmp_path.rglob("*.json"))
    assert ledger.snapshot()["failed"] == 2


async def test_a_timeout_is_raised_to_the_adapter_and_counted(tmp_path) -> None:
    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    ledger = model_cache.Ledger()
    transport = model_cache.CachingTransport(httpx.MockTransport(timeout), tmp_path, ledger)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ReadTimeout):
            await _post(client, {"model": "typesafe/jev-1.13", "state": {"message": "oi"}})
    assert ledger.snapshot()["models"]["typesafe/jev-1.13"]["failed"] == 1


async def test_without_a_directory_every_request_goes_to_the_provider(tmp_path) -> None:
    provider = Provider()
    client, ledger = _client(provider, None)
    await _post(client, LUNA)
    await _post(client, LUNA)
    assert provider.calls == 2 and ledger.snapshot()["hits"] == 0
    assert ledger.snapshot()["spend_usd"] == 0.0004


async def test_identical_requests_in_flight_at_once_pay_once(tmp_path) -> None:
    provider = Provider(delay=0.05)
    client, ledger = _client(provider, tmp_path)
    decision = {
        "model": "typesafe/jev-1.13",
        "state": {"message": "obrigado"},
        "questions": {"injection": {}},
    }
    provider.body = {"answers": {"injection": {"type": "noul", "noul": 0.02}}, "usage": {"cost": 0.000016}}
    answers = await asyncio.gather(*(_post(client, decision) for _ in range(4)))
    assert provider.calls == 1 and {json.dumps(a.json()) for a in answers} == {json.dumps(provider.body)}
    assert ledger.snapshot()["hits"] == 3


def test_every_answer_counts_for_its_purpose_fetched_or_from_disk(tmp_path: Path) -> None:
    assert model_cache.purpose(LUNA) == "openai/gpt-6-luna"
    assert model_cache.purpose({"questions": {"injection": {}}}) == "jev:injection"
    assert model_cache.purpose({"questions": {"answered_t1": {}, "injection": {}}}) == "jev:triage"

    async def go() -> dict:
        client, ledger = _client(Provider(), tmp_path)
        async with client:
            await _post(client, LUNA)
            await _post(client, LUNA)  # from disk: it still costs what it cost when fetched
        return ledger.snapshot()

    snapshot = asyncio.run(go())
    luna = snapshot["purposes"]["openai/gpt-6-luna"]
    assert (luna["calls"], luna["cost_usd"], luna["input_tokens"], luna["output_tokens"]) == (
        2,
        0.0004,
        200,
        40,
    )
