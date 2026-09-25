"""Google GenAI: the real `genai.Client` over a recorded HTTP transport (JSON and server-sent
events in the Gemini API's public format), and the emulator behind."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

genai = pytest.importorskip("google.genai")

from google.genai import types

from niadra import AsyncNiadra, Niadra
from niadra.integrations.google_genai import wrap
from niadra_mock import MockApp
from tests.integrations.support import EARLIER, MARINA, seed, seed_async, turns

RESPONSE = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "Your new lid ships today."}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 1400,
        "cachedContentTokenCount": 1024,
        "candidatesTokenCount": 9,
        "totalTokenCount": 1409,
    },
    "modelVersion": "gemini-2.5-flash",
}


def chunk(text: str, last: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}
    if last:
        body["usageMetadata"] = RESPONSE["usageMetadata"]
        body["modelVersion"] = "gemini-2.5-flash"
    return body


class Recorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if "streamGenerateContent" in request.url.path:
            events = [chunk("Your new lid "), chunk("ships today.", last=True)]
            data = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
            return httpx.Response(200, content=data.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=RESPONSE)


def client(recorder: Recorder) -> Any:
    transport = httpx.MockTransport(recorder)
    options = types.HttpOptions(
        httpx_client=httpx.Client(transport=transport),
        httpx_async_client=httpx.AsyncClient(transport=transport),
    )
    return genai.Client(api_key="test", http_options=options)


def test_the_pack_follows_your_system_instruction_and_the_answer_is_recorded(
    on_mock: Niadra, mock_app: MockApp
) -> None:
    seed(on_mock)
    recorder = Recorder()
    gemini = wrap(client(recorder))
    config = types.GenerateContentConfig(system_instruction="You are Acme's agent.")
    with on_mock.conversation("chat-1", subject=MARINA):
        response = gemini.models.generate_content(
            model="gemini-2.5-flash", contents="About my lid", config=config
        )
    assert response.text == "Your new lid ships today."
    on_mock.flush()
    system = recorder.requests[0]["systemInstruction"]["parts"]
    joined = " ".join(p["text"] for p in system)
    assert joined.startswith("You are Acme's agent.") and EARLIER in joined
    assert config.system_instruction == "You are Acme's agent.", "your config is not changed"
    assert turns(mock_app.cell, "chat-1") == [("ai_agent", "Your new lid ships today.")]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None
    assert (answer.usage.provider, answer.usage.model, answer.usage.cached_tokens) == (
        "google",
        "gemini-2.5-flash",
        1024,
    )


def test_news_goes_after_the_contents_and_a_stream_is_recorded(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    recorder = Recorder()
    gemini = wrap(client(recorder))
    history = [types.Content(role="user", parts=[types.Part(text="About my lid")])]
    with on_mock.conversation("chat-2", subject=MARINA) as conversation:
        conversation.context()
        on_mock.track(
            {
                "channel": "voice",
                "conversation_id": "call-9",
                "handles": [MARINA],
                "speaker": {"role": "customer"},
                "content": {"text": "I called about the lid"},
            }
        )
        on_mock.flush()
        parts = [
            c.text for c in gemini.models.generate_content_stream(model="gemini-2.5-flash", contents=history)
        ]
    assert "".join(parts) == "Your new lid ships today."
    on_mock.flush()
    last = recorder.requests[0]["contents"][-1]
    assert last["role"] == "user" and "I called about the lid" in last["parts"][0]["text"]
    assert len(history) == 1
    assert turns(mock_app.cell, "chat-2") == [("ai_agent", "Your new lid ships today.")]


async def test_the_async_client_and_the_agents_notes(on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    await seed_async(on_mock_async)
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    recorder = Recorder()
    gemini = wrap(client(recorder), agent_memory=True)
    async with on_mock_async.conversation("chat-3", subject=MARINA):
        await gemini.aio.models.generate_content(model="gemini-2.5-flash", contents="Hi")
        stream = await gemini.aio.models.generate_content_stream(model="gemini-2.5-flash", contents="Hi")
        async for _ in stream:
            pass
    await on_mock_async.flush()
    system = " ".join(p["text"] for p in recorder.requests[0]["systemInstruction"]["parts"])
    assert system.index("replacement order") < system.index(EARLIER)
    assert len(turns(mock_app.cell, "chat-3")) == 2


def test_outside_a_conversation_or_with_niadra_down_calls_pass(on_mock: Niadra, mock_app: MockApp) -> None:
    recorder = Recorder()
    gemini = wrap(client(recorder))
    gemini.models.generate_content(model="gemini-2.5-flash", contents="Hi")
    mock_app.cell.fail_next("/v1/", 503, times=100)
    with on_mock.conversation("chat-4", subject=MARINA):
        response = gemini.models.generate_content(
            model="gemini-2.5-flash", contents="Hi", config={"temperature": 0}
        )
    assert response.text == "Your new lid ships today."
    assert all("systemInstruction" not in r for r in recorder.requests)
