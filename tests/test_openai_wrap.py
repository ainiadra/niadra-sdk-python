"""`wrap()` against stand-ins shaped like the OpenAI client: `chat.completions.create`,
responses with `choices[0].message.content`, and streams of `choices[0].delta.content`."""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import respx

from niadra import AsyncNiadra, Niadra, phone, wrap
from tests.conftest import BASE, KEY, batch_ok, context_payload

MARINA = phone("+5511912345678")
LIVE = [
    {
        "at": "2026-09-22T14:02:00Z",
        "channel": "voice",
        "kind": "message",
        "speaker": "customer",
        "text": "called about the invoice",
        "source_id": "s",
    }
]


def completion(text: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def chunk(text: str | None, index: int = 0) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(index=index, delta=SimpleNamespace(content=text))])


class FakeStream:
    def __init__(self, parts: list[str | None]) -> None:
        self.parts = parts
        self.closed = False
        self.response = "raw"

    def __iter__(self) -> Iterator[Any]:
        return (chunk(p) for p in self.parts)

    def close(self) -> None:
        self.closed = True


class FakeAsyncStream(FakeStream):
    async def __aiter__(self) -> AsyncIterator[Any]:
        for part in self.parts:
            yield chunk(part)

    async def close(self) -> None:  # type: ignore[override]
        self.closed = True


class FakeCompletions:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            return FakeStream(["Your ", "credit ", None, "is issued."])
        return completion("Your credit is issued.")


class FakeAsyncCompletions(FakeCompletions):
    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            return FakeAsyncStream(["Async ", "answer"])
        return completion("Async answer")


class FakeOpenAI:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)
        self.beta: Any = None
        self.models = "untouched"


def sent(route: respx.Route) -> list[dict[str, Any]]:
    return [i for call in route.calls for i in json.loads(call.request.content)["items"]]


def test_outside_a_conversation_calls_pass_through(respx_mock: respx.MockRouter) -> None:
    completions = FakeCompletions()
    client = wrap(FakeOpenAI(completions))
    messages = [{"role": "user", "content": "hi"}]
    client.chat.completions.create(model="m", messages=messages)
    assert completions.requests[0]["messages"] == messages
    assert client.models == "untouched"
    assert not respx_mock.calls


def test_injects_after_instructions_and_records_the_answer(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload(live=LIVE))
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp", strict=True)
    completions = FakeCompletions()
    openai = wrap(FakeOpenAI(completions))
    messages = [{"role": "system", "content": "You are Acme's agent."}, {"role": "user", "content": "hi"}]
    with niadra.conversation("c-1", subject=MARINA) as conversation:
        openai.chat.completions.create(model="m", messages=messages)
        assert conversation.context_injected_at is not None
    niadra.flush()
    roles = [m["role"] for m in completions.requests[0]["messages"]]
    contents = [m["content"] for m in completions.requests[0]["messages"]]
    assert roles == ["system", "system", "user", "system"]
    assert contents[0] == "You are Acme's agent."
    assert contents[1].startswith("<context")
    assert "called about the invoice" in contents[3]
    assert messages == [
        {"role": "system", "content": "You are Acme's agent."},
        {"role": "user", "content": "hi"},
    ]
    agent = sent(batch)[0]
    assert agent["speaker"]["role"] == "ai_agent"
    assert agent["content"]["text"] == "Your credit is issued."
    assert agent["context_stamp"]["etag"] == "etag-1"
    assert agent["context_stamp"]["injected_at"] <= agent["occurred_at"]
    niadra.close()


def test_streams_are_recorded_when_they_end(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp", strict=True)
    openai = wrap(FakeOpenAI(FakeCompletions()))
    with niadra.conversation("c-1", subject=MARINA):
        stream = openai.chat.completions.create(model="m", messages=[], stream=True)
        assert stream.response == "raw"
        assert "".join(c.choices[0].delta.content or "" for c in stream) == "Your credit is issued."
    niadra.flush()
    assert sent(batch)[0]["content"]["text"] == "Your credit is issued."
    niadra.close()


def test_a_stream_closed_early_records_what_arrived(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp", strict=True)
    openai = wrap(FakeOpenAI(FakeCompletions()))
    with (
        niadra.conversation("c-1", subject=MARINA),
        openai.chat.completions.create(model="m", messages=[], stream=True) as stream,
    ):
        next(iter(stream))
    assert stream._stream.closed
    niadra.flush()
    assert sent(batch)[0]["content"]["text"] == "Your "
    niadra.close()


def test_a_failing_context_never_blocks_the_model_call(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(503)
    respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp")
    completions = FakeCompletions()
    openai = wrap(FakeOpenAI(completions))
    with niadra.conversation("c-1", subject=MARINA) as conversation:
        response = openai.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
        assert conversation.context_injected_at is None
    assert response.choices[0].message.content == "Your credit is issued."
    assert completions.requests[0]["messages"] == [{"role": "user", "content": "hi"}]
    niadra.close()


async def test_async_clients_inject_and_record(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    completions = FakeAsyncCompletions()
    openai = wrap(FakeOpenAI(completions))
    async with (
        AsyncNiadra(KEY, channel="app", strict=True) as niadra,
        niadra.conversation("c-1", subject=MARINA),
    ):
        await openai.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
        stream = await openai.chat.completions.create(model="m", messages=[], stream=True)
        parts = [c.choices[0].delta.content async for c in stream]
    assert parts == ["Async ", "answer"]
    assert completions.requests[0]["messages"][0]["content"].startswith("<context")
    texts = [i["content"]["text"] for i in sent(batch) if i["type"] == "event"]
    assert texts == ["Async answer", "Async answer"]


def test_a_sync_client_ignores_an_async_conversation(respx_mock: respx.MockRouter) -> None:
    completions = FakeCompletions()
    openai = wrap(FakeOpenAI(completions))
    conversation = AsyncNiadra(KEY, channel="app").conversation("c-1", subject=MARINA)
    wrapped = wrap(FakeOpenAI(completions), conversation=conversation)
    wrapped.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    openai.chat.completions.create(model="m", messages=[])
    assert completions.requests[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert not respx_mock.calls


def required_args(func: Any) -> Any:
    """What the OpenAI client does to its methods: a plain wrapper that hides `async def`."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return wrapper


class DecoratedAsyncCompletions(FakeCompletions):
    @required_args
    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return completion("Decorated answer")


class RawResponse:
    def __init__(self, parsed: Any) -> None:
        self._parsed = parsed
        self.headers = {"x-request-id": "req-1"}

    def parse(self) -> Any:
        return self._parsed


class RawCompletions:
    def __init__(self, completions: FullCompletions) -> None:
        self._completions = completions

    def create(self, **kwargs: Any) -> RawResponse:
        return RawResponse(self._completions.create(**kwargs))

    def parse(self, **kwargs: Any) -> RawResponse:
        return RawResponse(self._completions.parse(**kwargs))


class FullCompletions(FakeCompletions):
    def __init__(self) -> None:
        super().__init__()
        self.with_raw_response = RawCompletions(self)

    def parse(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return completion('{"intent": "refund"}')


class ManyChoicesCompletions(FakeCompletions):
    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return FakeStreamOf([chunk("first ", 0), chunk("second ", 1), chunk("answer", 0)])


class FakeStreamOf(FakeStream):
    def __init__(self, chunks: list[Any]) -> None:
        super().__init__([])
        self.chunks = chunks

    def __iter__(self) -> Iterator[Any]:
        return iter(self.chunks)


def test_decorated_async_methods_are_still_awaited(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    completions = DecoratedAsyncCompletions()
    openai = wrap(FakeOpenAI(completions))

    async def run() -> None:
        async with (
            AsyncNiadra(KEY, channel="app", strict=True) as niadra,
            niadra.conversation("c-1", subject=MARINA),
        ):
            response = await openai.chat.completions.create(model="m", messages=[])
            assert response.choices[0].message.content == "Decorated answer"

    asyncio.run(run())
    assert completions.requests[0]["messages"][0]["content"].startswith("<context")
    assert [i["content"]["text"] for i in sent(batch) if i["type"] == "event"] == ["Decorated answer"]


def test_raw_responses_are_recorded_when_parsed(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp", strict=True)
    completions = FullCompletions()
    openai = wrap(FakeOpenAI(completions))
    with niadra.conversation("c-1", subject=MARINA):
        raw = openai.chat.completions.with_raw_response.create(model="m", messages=[])
        assert raw.headers == {"x-request-id": "req-1"}
        assert raw.parse().choices[0].message.content == "Your credit is issued."
        raw.parse()
        streamed = openai.chat.completions.with_raw_response.create(model="m", messages=[], stream=True)
        assert "".join(c.choices[0].delta.content or "" for c in streamed.parse()) == "Your credit is issued."
    niadra.flush()
    assert completions.requests[0]["messages"][0]["content"].startswith("<context")
    texts = [i["content"]["text"] for i in sent(batch) if i["type"] == "event"]
    assert texts == ["Your credit is issued.", "Your credit is issued."], "each call is recorded once"
    niadra.close()


def test_structured_output_parse_is_intercepted(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp", strict=True)
    completions = FullCompletions()
    client = FakeOpenAI(completions)
    client.beta = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    openai = wrap(client)
    with niadra.conversation("c-1", subject=MARINA):
        openai.chat.completions.parse(model="m", messages=[])
        openai.beta.chat.completions.parse(model="m", messages=[])
    niadra.flush()
    assert all(r["messages"][0]["content"].startswith("<context") for r in completions.requests)
    texts = [i["content"]["text"] for i in sent(batch) if i["type"] == "event"]
    assert texts == ['{"intent": "refund"}'] * 2
    niadra.close()


def test_streams_record_only_the_first_choice_and_support_next(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp", strict=True)
    openai = wrap(FakeOpenAI(ManyChoicesCompletions()))
    with niadra.conversation("c-1", subject=MARINA):
        stream = openai.chat.completions.create(model="m", messages=[], stream=True, n=2)
        assert next(stream).choices[0].delta.content == "first "
        assert [c.choices[0].delta.content for c in stream] == ["second ", "answer"]
    niadra.flush()
    assert sent(batch)[0]["content"]["text"] == "first answer"
    niadra.close()


def test_a_failing_capture_never_breaks_the_model_call(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    niadra = Niadra(KEY, strict=True)  # no channel: recording the turn fails, loudly under strict
    niadra._closed = True
    openai = wrap(FakeOpenAI(FakeCompletions()))
    with niadra.conversation("c-1", subject=MARINA):
        response = openai.chat.completions.create(model="m", messages=[])
        stream = openai.chat.completions.create(model="m", messages=[], stream=True)
        assert "".join(c.choices[0].delta.content or "" for c in stream) == "Your credit is issued."
    assert response.choices[0].message.content == "Your credit is issued."
    warnings = [r.getMessage() for r in caplog.records if r.name == "niadra"]
    assert warnings and all("credit" not in message for message in warnings)


def test_agent_memory_goes_before_the_customer_context(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    notes = respx_mock.get(f"{BASE}/v1/agent-memory/block").respond(
        200,
        json={
            "text": "<agent_memory>Refunds need the order number.</agent_memory>",
            "etag": "am-1",
            "enabled": True,
        },
    )
    respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp", strict=True)
    completions = FakeCompletions()
    openai = wrap(FakeOpenAI(completions), agent_memory=True)
    messages = [{"role": "system", "content": "You are Acme's agent."}, {"role": "user", "content": "hi"}]
    with niadra.conversation("c-1", subject=MARINA):
        openai.chat.completions.create(model="m", messages=messages)
    system = completions.requests[0]["messages"][1]["content"]
    assert notes.called
    assert system.startswith("<agent_memory>Refunds need the order number.</agent_memory>\n\n<context")
    niadra.close()


def test_agent_memory_off_in_the_space_leaves_only_the_context(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    respx_mock.get(f"{BASE}/v1/agent-memory/block").respond(
        200, json={"text": "", "etag": "am-off", "enabled": False}
    )
    respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="whatsapp", strict=True)
    completions = FakeCompletions()
    openai = wrap(FakeOpenAI(completions), agent_memory={"max_tokens": 200, "tags": ["refunds"]})
    with niadra.conversation("c-1", subject=MARINA):
        openai.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert completions.requests[0]["messages"][0]["content"].startswith("<context")
    niadra.close()
