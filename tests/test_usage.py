"""The provider's usage of each model call, sent with the agent's turn: read by `wrap()` from OpenAI
responses and streams, or passed with `agent(usage=...)` from an OpenAI or Anthropic response."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import respx

from niadra import EventItem, ModelUsage, Niadra, phone, wrap
from niadra.usage import provider_of, token_counts
from tests.conftest import BASE, KEY, batch_ok, context_payload

MARINA = phone("+5511912345678")


def openai_completion(text: str, *, prompt: int = 3000, cached: int | None = 2048) -> Any:
    details = None if cached is None else SimpleNamespace(cached_tokens=cached)
    return SimpleNamespace(
        model="gpt-4.1-2025-04-14",
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=40, prompt_tokens_details=details),
    )


def anthropic_message() -> Any:
    usage = SimpleNamespace(
        input_tokens=120, cache_read_input_tokens=4000, cache_creation_input_tokens=500, output_tokens=60
    )
    return SimpleNamespace(type="message", model="claude-sonnet-4-5-20250929", usage=usage)


class Completions:
    def __init__(self, response: Any = None, chunks: list[Any] | None = None) -> None:
        self.response = response
        self.chunks = chunks or []

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return iter(self.chunks)
        return self.response


def client(completions: Completions) -> Any:
    return wrap(SimpleNamespace(chat=SimpleNamespace(completions=completions), beta=None))


def sent(route: respx.Route) -> list[dict[str, Any]]:
    return [i for call in route.calls for i in json.loads(call.request.content)["items"]]


@pytest.fixture
def cell(respx_mock: respx.MockRouter) -> Iterator[tuple[Niadra, respx.Route]]:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp", strict=True)
    yield niadra, batch
    niadra.close()


def test_wrap_sends_the_usage_of_each_call_with_the_agents_turn(cell: tuple[Niadra, respx.Route]) -> None:
    niadra, batch = cell
    openai = client(Completions(openai_completion("Your credit is issued.")))
    with niadra.conversation("c-1", subject=MARINA):
        openai.chat.completions.create(model="gpt-4.1", messages=[{"role": "user", "content": "hi"}])
    niadra.flush()
    [agent] = [i for i in sent(batch) if i.get("speaker", {}).get("role") == "ai_agent"]
    assert agent["usage"] == {
        "provider": "openai",
        "model": "gpt-4.1-2025-04-14",
        "prompt_tokens": 3000,
        "cached_tokens": 2048,
        "cache_write_tokens": 0,
    }


def test_a_stream_reports_its_usage_when_the_caller_asked_for_it(cell: tuple[Niadra, respx.Route]) -> None:
    niadra, batch = cell

    def piece(text: str | None, usage: Any = None) -> Any:
        choices = [] if text is None else [SimpleNamespace(index=0, delta=SimpleNamespace(content=text))]
        return SimpleNamespace(model="gpt-4.1-mini-2025-04-14", choices=choices, usage=usage)

    last = SimpleNamespace(prompt_tokens=1800, prompt_tokens_details=SimpleNamespace(cached_tokens=1024))
    openai = client(Completions(chunks=[piece("Your "), piece("credit."), piece(None, last)]))
    with niadra.conversation("c-2", subject=MARINA):
        stream = openai.chat.completions.create(model="m", messages=[], stream=True)
        assert [c.choices[0].delta.content for c in stream if c.choices] == ["Your ", "credit."]
    niadra.flush()
    [agent] = [i for i in sent(batch) if i.get("speaker", {}).get("role") == "ai_agent"]
    assert agent["content"]["text"] == "Your credit."
    assert (agent["usage"]["model"], agent["usage"]["cached_tokens"]) == ("gpt-4.1-mini-2025-04-14", 1024)


def test_a_call_without_usage_records_the_turn_without_it(cell: tuple[Niadra, respx.Route]) -> None:
    niadra, batch = cell
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    with niadra.conversation("c-3", subject=MARINA):
        client(Completions(response)).chat.completions.create(model="m", messages=[])
    niadra.flush()
    [agent] = [i for i in sent(batch) if i.get("speaker", {}).get("role") == "ai_agent"]
    assert "usage" not in agent


def test_agents_without_wrap_pass_the_response_with_the_turn(cell: tuple[Niadra, respx.Route]) -> None:
    niadra, batch = cell
    with niadra.conversation("c-4", subject=MARINA) as conversation:
        conversation.agent("Your visit is tomorrow.", usage=anthropic_message())
        conversation.agent(
            "And the invoice is credited.", usage=ModelUsage.from_response(openai_completion("x"))
        )
        conversation.agent("A turn whose usage cannot be read is still recorded.", usage=object())
    niadra.flush()
    agents = [i for i in sent(batch) if i.get("speaker", {}).get("role") == "ai_agent"]
    assert agents[0]["usage"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5-20250929",
        "prompt_tokens": 4620,
        "cached_tokens": 4000,
        "cache_write_tokens": 500,
    }
    assert agents[1]["usage"]["provider"] == "openai"
    assert "usage" not in agents[2]


def test_the_shapes_the_usage_reader_understands() -> None:
    openai = {"prompt_tokens": 3000, "prompt_tokens_details": {"cached_tokens": 2048}}
    assert token_counts(openai) == {"prompt_tokens": 3000, "cached_tokens": 2048, "cache_write_tokens": 0}
    anthropic = {"input_tokens": 120, "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 500}
    assert token_counts(anthropic) == {
        "prompt_tokens": 4620,
        "cached_tokens": 4000,
        "cache_write_tokens": 500,
    }
    responses = {"input_tokens": 2500, "input_tokens_details": {"cached_tokens": 1200}}
    assert token_counts(responses) == {"prompt_tokens": 2500, "cached_tokens": 1200, "cache_write_tokens": 0}
    gateway = {"prompt_tokens": 120, "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 500}
    assert token_counts(gateway) == {"prompt_tokens": 4500, "cached_tokens": 4000, "cache_write_tokens": 500}
    assert token_counts({"completion_tokens": 5}) is None
    assert token_counts(None) is None
    assert provider_of("anthropic/claude-sonnet-4.5") == "anthropic"
    assert provider_of("claude-haiku-4-5") == "anthropic"
    assert provider_of("gpt-4.1") == "openai"
    assert ModelUsage.from_response({"prompt_tokens": 10}) is None  # no model to name
    bare = ModelUsage.from_response({"prompt_tokens": 10}, model="gpt-4o", provider="Azure")
    assert bare is not None
    assert bare.provider == "azure"


def test_usage_rides_only_on_the_agents_message() -> None:
    usage = ModelUsage(provider="openai", model="gpt-4.1", prompt_tokens=10, cached_tokens=5)
    base: dict[str, Any] = {"channel": "whatsapp", "handles": [MARINA], "content": {"text": "hi"}}
    EventItem(**base, speaker={"role": "ai_agent"}, usage=usage)
    with pytest.raises(ValueError, match="only valid on a message of the `ai_agent`"):
        EventItem(**base, speaker={"role": "customer"}, usage=usage)
    with pytest.raises(ValueError, match="part of prompt_tokens"):
        ModelUsage(
            provider="openai", model="gpt-4.1", prompt_tokens=10, cached_tokens=8, cache_write_tokens=5
        )


def test_usage_given_by_hand_is_kept_or_read_never_dropping_the_turn(
    cell: tuple[Niadra, respx.Route],
) -> None:
    niadra, batch = cell
    with niadra.conversation("c-5", subject=MARINA) as conversation:
        conversation.agent(
            "one", usage={"provider": "openai", "model": "gpt-4.1", "prompt_tokens": 10, "cached_tokens": 4}
        )
        conversation.agent("two", usage={"provider": "Open AI", "model": "gpt-4.1", "prompt_tokens": 10})
    niadra.flush()
    agents = [i for i in sent(batch) if i.get("speaker", {}).get("role") == "ai_agent"]
    assert [a["content"]["text"] for a in agents] == ["one", "two"]
    assert agents[0]["usage"] == {
        "provider": "openai",
        "model": "gpt-4.1",
        "prompt_tokens": 10,
        "cached_tokens": 4,
        "cache_write_tokens": 0,
    }
    assert agents[1]["usage"]["provider"] == "openai"
