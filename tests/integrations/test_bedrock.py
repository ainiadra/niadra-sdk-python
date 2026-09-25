"""Amazon Bedrock Converse: a real boto3 `bedrock-runtime` client with botocore's Stubber (no
network, no account), responses in the Converse API's shapes, and the emulator behind."""

from __future__ import annotations

from typing import Any

import pytest

boto3 = pytest.importorskip("boto3")

from botocore.stub import ANY, Stubber

from niadra import Niadra
from niadra.integrations.bedrock import wrap
from niadra_mock import MockApp
from tests.integrations.support import EARLIER, MARINA, seed, turns

MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
HISTORY = [{"role": "user", "content": [{"text": "About my lid"}]}]
RESPONSE = {
    "output": {"message": {"role": "assistant", "content": [{"text": "Your new lid ships today."}]}},
    "stopReason": "end_turn",
    "usage": {
        "inputTokens": 180,
        "outputTokens": 12,
        "totalTokens": 1216,
        "cacheReadInputTokens": 1024,
        "cacheWriteInputTokens": 0,
    },
    "metrics": {"latencyMs": 420},
}


def client() -> Any:
    return boto3.client(
        "bedrock-runtime", region_name="us-east-2", aws_access_key_id="test", aws_secret_access_key="test"
    )


def test_the_pack_follows_your_system_and_the_answer_is_recorded(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    raw = client()
    sent: list[dict[str, Any]] = []
    raw.meta.events.register(
        "provide-client-params.bedrock-runtime.Converse", lambda params, **_: sent.append(params)
    )
    with Stubber(raw) as stub:
        stub.add_response("converse", RESPONSE, {"modelId": MODEL, "messages": ANY, "system": ANY})
        bedrock = wrap(raw)
        with on_mock.conversation("chat-1", subject=MARINA):
            response = bedrock.converse(
                modelId=MODEL, system=[{"text": "You are Acme's agent."}], messages=HISTORY
            )
    assert response["output"]["message"]["content"][0]["text"] == "Your new lid ships today."
    on_mock.flush()
    system = sent[0]["system"]
    assert (
        system[0] == {"text": "You are Acme's agent."} and EARLIER in system[1]["text"] and len(system) == 2
    )
    assert turns(mock_app.cell, "chat-1") == [("ai_agent", "Your new lid ships today.")]
    answer = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert answer.usage is not None
    assert (answer.usage.provider, answer.usage.model, answer.usage.prompt_tokens) == ("bedrock", MODEL, 1204)
    assert answer.usage.cached_tokens == 1024


def test_a_cache_point_follows_the_pack_when_you_use_one(on_mock: Niadra) -> None:
    seed(on_mock)
    raw = client()
    sent: list[dict[str, Any]] = []
    raw.meta.events.register(
        "provide-client-params.bedrock-runtime.Converse", lambda params, **_: sent.append(params)
    )
    system = [{"text": "You are Acme's agent."}, {"cachePoint": {"type": "default"}}]
    with Stubber(raw) as stub:
        stub.add_response("converse", RESPONSE, {"modelId": MODEL, "messages": ANY, "system": ANY})
        with on_mock.conversation("chat-2", subject=MARINA):
            wrap(raw).converse(modelId=MODEL, system=system, messages=HISTORY)
    assert sent[0]["system"][-1] == {"cachePoint": {"type": "default"}}
    assert EARLIER in sent[0]["system"][-2]["text"]


def test_news_joins_the_last_user_message(on_mock: Niadra) -> None:
    seed(on_mock)
    raw = client()
    sent: list[dict[str, Any]] = []
    raw.meta.events.register(
        "provide-client-params.bedrock-runtime.Converse", lambda params, **_: sent.append(params)
    )
    with Stubber(raw) as stub:
        stub.add_response("converse", RESPONSE, {"modelId": MODEL, "messages": ANY, "system": ANY})
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
            wrap(raw).converse(modelId=MODEL, messages=HISTORY)
    content = sent[0]["messages"][-1]["content"]
    assert content[0] == {"text": "About my lid"} and "I called about the lid" in content[-1]["text"]
    assert HISTORY == [{"role": "user", "content": [{"text": "About my lid"}]}]


def test_a_stream_is_recorded_when_it_ends(
    on_mock: Niadra, mock_app: MockApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = client()
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Your new lid "}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": "ships today."}, "contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": RESPONSE["usage"], "metrics": {"latencyMs": 400}}},
    ]
    monkeypatch.setattr(raw, "converse_stream", lambda **kwargs: {"stream": iter(events)})
    with on_mock.conversation("chat-4", subject=MARINA):
        response = wrap(raw).converse_stream(modelId=MODEL, messages=HISTORY)
        assert len(list(response["stream"])) == 5
    on_mock.flush()
    assert turns(mock_app.cell, "chat-4") == [("ai_agent", "Your new lid ships today.")]


def test_outside_a_conversation_or_with_niadra_down_calls_pass(on_mock: Niadra, mock_app: MockApp) -> None:
    raw = client()
    sent: list[dict[str, Any]] = []
    raw.meta.events.register(
        "provide-client-params.bedrock-runtime.Converse", lambda params, **_: sent.append(params)
    )
    with Stubber(raw) as stub:
        stub.add_response("converse", RESPONSE, {"modelId": MODEL, "messages": ANY})
        stub.add_response("converse", RESPONSE, {"modelId": MODEL, "messages": ANY})
        bedrock = wrap(raw)
        bedrock.converse(modelId=MODEL, messages=HISTORY)
        mock_app.cell.fail_next("/v1/", 503, times=100)
        with on_mock.conversation("chat-5", subject=MARINA):
            bedrock.converse(modelId=MODEL, messages=HISTORY)
    assert all("system" not in params for params in sent)
    assert bedrock.meta.service_model.service_name == "bedrock-runtime"
