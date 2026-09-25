"""LiteLLM: real `litellm.completion` calls with LiteLLM's own `mock_response` (no provider, no
network), a logger that captures what LiteLLM was asked, and the emulator behind."""

from __future__ import annotations

from typing import Any

import pytest

litellm = pytest.importorskip("litellm")

from litellm.integrations.custom_logger import CustomLogger

from niadra import AsyncNiadra, Niadra
from niadra.integrations.litellm import NiadraLogger, acompletion, completion
from niadra_mock import MockApp
from tests.integrations.support import EARLIER, MARINA, seed, seed_async, turns

MESSAGES = [
    {"role": "system", "content": "You are Acme's agent."},
    {"role": "user", "content": "About my lid"},
]


class Seen(CustomLogger):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[list[dict[str, Any]]] = []

    def log_pre_api_call(self, model: Any, messages: Any, kwargs: Any) -> None:
        self.messages.append(list(messages))


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> Seen:
    logger = Seen()
    monkeypatch.setattr(litellm, "callbacks", [logger])
    return logger


def test_completion_places_the_pack_and_records_the_answer(
    on_mock: Niadra, mock_app: MockApp, seen: Seen
) -> None:
    seed(on_mock)
    with on_mock.conversation("chat-1", subject=MARINA):
        response = completion(
            model="anthropic/claude-sonnet-4-5", messages=MESSAGES, mock_response="Your new lid ships today."
        )
    assert response.choices[0].message.content == "Your new lid ships today."
    on_mock.flush()
    sent = seen.messages[0]
    assert sent[0] == MESSAGES[0] and sent[1]["role"] == "system" and EARLIER in sent[1]["content"]
    assert len(MESSAGES) == 2, "your messages are not changed"
    assert turns(mock_app.cell, "chat-1") == [("ai_agent", "Your new lid ships today.")]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None and answer.usage.provider == "anthropic"
    assert answer.context_stamp is not None


def test_a_stream_is_recorded_when_it_ends(on_mock: Niadra, mock_app: MockApp, seen: Seen) -> None:
    with on_mock.conversation("chat-2", subject=MARINA):
        stream = completion(model="gpt-4.1", messages=MESSAGES, mock_response="Streamed answer.", stream=True)
        text = "".join(chunk.choices[0].delta.content or "" for chunk in stream)
    assert text == "Streamed answer."
    on_mock.flush()
    assert turns(mock_app.cell, "chat-2") == [("ai_agent", "Streamed answer.")]


async def test_acompletion_with_the_agents_notes(
    on_mock_async: AsyncNiadra, mock_app: MockApp, seen: Seen
) -> None:
    await seed_async(on_mock_async)
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    async with on_mock_async.conversation("chat-3", subject=MARINA):
        await acompletion(model="gpt-4.1", messages=MESSAGES, mock_response="Opening it.", agent_memory=True)
    await on_mock_async.flush()
    slot = seen.messages[0][1]["content"]
    assert slot.index("replacement order") < slot.index(EARLIER)
    assert turns(mock_app.cell, "chat-3") == [("ai_agent", "Opening it.")]


def test_the_logger_records_calls_made_directly(
    on_mock: Niadra, mock_app: MockApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(litellm, "callbacks", [NiadraLogger()])
    with on_mock.conversation("chat-4", subject=MARINA):
        litellm.completion(model="gpt-4.1", messages=MESSAGES, mock_response="Direct answer.")
        completion(model="gpt-4.1", messages=MESSAGES, mock_response="Wrapped answer.")
    litellm.completion(model="gpt-4.1", messages=MESSAGES, mock_response="Outside any conversation.")
    on_mock.flush()
    assert turns(mock_app.cell, "chat-4") == [("ai_agent", "Direct answer."), ("ai_agent", "Wrapped answer.")]


def test_niadra_down_or_no_conversation_leaves_the_call_alone(
    on_mock: Niadra, mock_app: MockApp, seen: Seen
) -> None:
    completion(model="gpt-4.1", messages=MESSAGES, mock_response="ok")
    mock_app.cell.fail_next("/v1/", 503, times=100)
    with on_mock.conversation("chat-5", subject=MARINA):
        response = completion(model="gpt-4.1", messages=MESSAGES, mock_response="ok")
    assert response.choices[0].message.content == "ok"
    assert seen.messages == [MESSAGES, MESSAGES]
