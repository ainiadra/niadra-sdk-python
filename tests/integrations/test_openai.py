"""`wrap()` with the real OpenAI clients, Azure OpenAI's included, over a recorded HTTP transport
(chat completions in the public format), and the emulator behind. The `wrap()` of 0.1 covers Azure
as it is: `AzureOpenAI` has the same shape as `OpenAI`."""

from __future__ import annotations

import json
from typing import Any

import pytest

openai = pytest.importorskip("openai")
# The OpenAI SDK 3.x runs on httpx2, its own fork of httpx.
httpx = pytest.importorskip("httpx2")

from niadra import AsyncNiadra, Niadra, wrap
from niadra_mock import MockApp
from tests.integrations.support import EARLIER, MARINA, seed, seed_async, turns

COMPLETION = {
    "id": "chatcmpl-9",
    "object": "chat.completion",
    "created": 1790301600,
    "model": "gpt-4.1-2025-04-14",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Your new lid ships today."},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 1400,
        "completion_tokens": 9,
        "total_tokens": 1409,
        "prompt_tokens_details": {"cached_tokens": 1024},
    },
}
MESSAGES = [
    {"role": "system", "content": "You are Acme's agent."},
    {"role": "user", "content": "About my lid"},
]


class Recorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json=COMPLETION)


def test_azure_openai_gets_the_pack_and_records_the_answer(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    recorder = Recorder()
    azure = openai.AzureOpenAI(
        api_key="test",
        api_version="2025-04-01-preview",
        azure_endpoint="https://acme.openai.azure.com",
        http_client=httpx.Client(transport=httpx.MockTransport(recorder)),
    )
    client = wrap(azure)
    with on_mock.conversation("azure-1", subject=MARINA):
        reply = client.chat.completions.create(model="support-gpt41", messages=MESSAGES)
    assert reply.choices[0].message.content == "Your new lid ships today."
    on_mock.flush()
    url, body = recorder.requests[0]
    assert "/openai/deployments/support-gpt41/chat/completions" in url
    assert body["messages"][0] == MESSAGES[0]
    assert body["messages"][1]["role"] == "system" and EARLIER in body["messages"][1]["content"]
    assert turns(mock_app.cell, "azure-1") == [("ai_agent", "Your new lid ships today.")]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None
    assert (answer.usage.model, answer.usage.prompt_tokens, answer.usage.cached_tokens) == (
        "gpt-4.1-2025-04-14",
        1400,
        1024,
    )


async def test_the_async_openai_client(on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    await seed_async(on_mock_async)
    recorder = Recorder()
    client = wrap(
        openai.AsyncOpenAI(
            api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(recorder))
        )
    )
    async with on_mock_async.conversation("oa-1", subject=MARINA):
        await client.chat.completions.create(model="gpt-4.1", messages=MESSAGES)
    await on_mock_async.flush()
    assert EARLIER in recorder.requests[0][1]["messages"][1]["content"]
    assert turns(mock_app.cell, "oa-1") == [("ai_agent", "Your new lid ships today.")]
