"""Anthropic: the real `Anthropic` and `AsyncAnthropic` clients over a recorded HTTP transport
(JSON and server-sent events in the Messages API's public format), and the emulator behind."""

from __future__ import annotations

import json
from typing import Any

import pytest

anthropic = pytest.importorskip("anthropic")
# The Anthropic SDK 1.x runs on httpx2, its own fork of httpx.
httpx = pytest.importorskip("httpx2")

from niadra import AsyncNiadra, Niadra
from niadra.integrations.anthropic import wrap
from niadra_mock import MockApp
from tests.integrations.support import EARLIER, MARINA, seed, seed_async, turns

USAGE = {
    "input_tokens": 180,
    "output_tokens": 12,
    "cache_read_input_tokens": 1024,
    "cache_creation_input_tokens": 0,
}
MESSAGE = {
    "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5-20250929",
    "content": [{"type": "text", "text": "Your new lid ships today."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": USAGE,
}


def sse() -> bytes:
    events = [
        (
            "message_start",
            {"type": "message_start", "message": {**MESSAGE, "content": [], "stop_reason": None}},
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Your new lid "},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ships today."},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 12},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class Recorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: Any) -> Any:
        body = json.loads(request.content)
        self.requests.append(body)
        if body.get("stream"):
            return httpx.Response(200, content=sse(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=MESSAGE)


def client(recorder: Recorder) -> Any:
    return anthropic.Anthropic(
        api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(recorder))
    )


def async_client(recorder: Recorder) -> Any:
    transport = httpx.MockTransport(recorder)
    return anthropic.AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=transport))


HISTORY = [{"role": "user", "content": "About my lid"}]


def test_the_pack_follows_your_system_and_the_answer_is_recorded(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    recorder = Recorder()
    claude = wrap(client(recorder))
    with on_mock.conversation("chat-1", subject=MARINA) as conversation:
        conversation.customer("About my lid")
        message = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=64, system="You are Acme's agent.", messages=HISTORY
        )
        assert message.content[0].text == "Your new lid ships today."
    on_mock.flush()
    sent = recorder.requests[0]
    assert sent["system"][0] == {"type": "text", "text": "You are Acme's agent."}
    assert EARLIER in sent["system"][1]["text"] and "cache_control" not in sent["system"][1]
    assert sent["messages"] == HISTORY, "no turn block without news from another channel"
    assert HISTORY == [{"role": "user", "content": "About my lid"}], "your arguments are not changed"
    assert turns(mock_app.cell, "chat-1")[-1] == ("ai_agent", "Your new lid ships today.")
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None
    assert (answer.usage.provider, answer.usage.prompt_tokens, answer.usage.cached_tokens) == (
        "anthropic",
        1204,
        1024,
    )
    assert answer.context_stamp is not None


def test_a_cached_system_prefix_keeps_the_pack_in_it(on_mock: Niadra) -> None:
    seed(on_mock)
    recorder = Recorder()
    system = [{"type": "text", "text": "You are Acme's agent.", "cache_control": {"type": "ephemeral"}}]
    with on_mock.conversation("chat-2", subject=MARINA):
        wrap(client(recorder)).messages.create(model="m", max_tokens=8, system=system, messages=HISTORY)
    assert recorder.requests[0]["system"][1]["cache_control"] == {"type": "ephemeral"}


def test_news_goes_at_the_end_of_the_last_user_message(on_mock: Niadra) -> None:
    seed(on_mock)
    recorder = Recorder()
    with on_mock.conversation("chat-3", subject=MARINA) as conversation:
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
        wrap(client(recorder)).messages.create(model="m", max_tokens=8, messages=HISTORY)
    last = recorder.requests[0]["messages"][-1]
    assert last["role"] == "user" and last["content"][0] == {"type": "text", "text": "About my lid"}
    assert "I called about the lid" in last["content"][-1]["text"]


def test_a_stream_is_recorded_when_it_ends(on_mock: Niadra, mock_app: MockApp) -> None:
    recorder = Recorder()
    claude = wrap(client(recorder))
    with on_mock.conversation("chat-4", subject=MARINA):
        events = claude.messages.create(model="m", max_tokens=8, messages=HISTORY, stream=True)
        assert [e.type for e in events][-1] == "message_stop"
        with claude.messages.stream(model="m", max_tokens=8, messages=HISTORY) as stream:
            assert "".join(stream.text_stream) == "Your new lid ships today."
    on_mock.flush()
    assert turns(mock_app.cell, "chat-4") == [("ai_agent", "Your new lid ships today.")] * 2


async def test_the_async_client(on_mock_async: AsyncNiadra, mock_app: MockApp) -> None:
    await seed_async(on_mock_async)
    recorder = Recorder()
    claude = wrap(async_client(recorder), agent_memory=True)
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    async with on_mock_async.conversation("chat-5", subject=MARINA):
        await claude.messages.create(model="m", max_tokens=8, messages=HISTORY)
        events = await claude.messages.create(model="m", max_tokens=8, messages=HISTORY, stream=True)
        async for _ in events:
            pass
        async with claude.messages.stream(model="m", max_tokens=8, messages=HISTORY) as stream:
            await stream.get_final_message()
    await on_mock_async.flush()
    system = recorder.requests[0]["system"]
    assert system.index("replacement order") < system.index(EARLIER)
    assert len(turns(mock_app.cell, "chat-5")) == 3


def test_outside_a_conversation_or_with_niadra_down_calls_pass(on_mock: Niadra, mock_app: MockApp) -> None:
    recorder = Recorder()
    claude = wrap(client(recorder))
    claude.messages.create(model="m", max_tokens=8, messages=HISTORY)
    assert "system" not in recorder.requests[0]
    mock_app.cell.fail_next("/v1/", 503, times=100)
    with on_mock.conversation("chat-6", subject=MARINA):
        message = claude.messages.create(model="m", max_tokens=8, system="x", messages=HISTORY)
    assert message.content[0].text == "Your new lid ships today."
    assert recorder.requests[1]["system"] == "x"
    assert claude.models is not None, "everything else is the client's own"
