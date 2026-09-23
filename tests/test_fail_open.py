from __future__ import annotations

import logging

import httpx
import pytest
import respx

import niadra._base
from niadra import AsyncNiadra, Niadra, ServerError, phone
from tests.conftest import BASE, KEY

MARINA = phone("+5511912345678")


@pytest.fixture(autouse=True)
def _reset_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(niadra._base, "_warned_no_key", False)


def test_without_a_key_every_method_is_a_quiet_no_op(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="niadra")
    client = Niadra()
    other = Niadra()
    assert not client.enabled
    assert caplog.text.count("client is disabled") == 1, "warns once per process"

    assert not client.context(MARINA, conversation_id="c-1")
    assert client.context(MARINA).error == "disabled"
    assert client.search(MARINA, "x").error == "disabled"
    assert client.timeline(MARINA).error == "disabled"
    assert client.open("ep_1") is None
    assert client.track({"speaker": {"role": "customer"}}) is False
    assert client.action("credit", subject=MARINA) is False
    assert client.identify([MARINA, MARINA]) is None
    assert client.verify("login", "V1", handle=MARINA) is None
    assert client.handoff("c-1", "human") is False
    assert client.subject_token(MARINA) is None
    assert client.tools(MARINA).names
    assert client.flush()
    with client.conversation(subject=MARINA, channel="chat") as conversation:
        assert not conversation.context()
        assert conversation.customer("hi") is False
    client.close()
    other.close()
    assert not respx_mock.calls


async def test_the_async_client_is_a_no_op_too() -> None:
    client = AsyncNiadra()
    assert not await client.context(MARINA)
    assert (await client.search(MARINA, "x")).error == "disabled"
    assert await client.subject_token(MARINA) is None
    assert client.track({"speaker": {"role": "customer"}}) is False
    await client.close()


def test_every_network_method_swallows_outages(respx_mock: respx.MockRouter) -> None:
    respx_mock.route(host="acme.br1.api.niadra.com").mock(side_effect=httpx.ConnectError("down"))
    client = Niadra(KEY, channel="chat")
    client._closed = True
    assert client.context(MARINA).error == "APIConnectionError"
    assert client.search(MARINA, "x").error == "APIConnectionError"
    assert client.timeline(MARINA).error == "APIConnectionError"
    assert client.open("ep_1") is None
    assert client.identify([MARINA, phone("+5511900000000")]) is None
    assert client.subject_token(MARINA) is None
    assert client.flush() is False


def test_strict_mode_raises_instead(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/history/search").respond(500)
    client = Niadra(KEY, strict=True)
    client._closed = True
    with pytest.raises(ServerError):
        client.search(MARINA, "x")
    with pytest.raises(ValueError, match="channel"):
        client.track({"speaker": {"role": "customer"}, "content": {"text": "hi"}, "handles": [MARINA]})


def test_logs_never_carry_the_customer(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="niadra")
    respx_mock.route(host="acme.br1.api.niadra.com").respond(
        422,
        json={
            "title": "invalid",
            "status": 422,
            "code": "invalid_input",
            "detail": "subject.value: string_type",
        },
    )
    client = Niadra(KEY, channel="chat")
    client._closed = True
    client.context(MARINA)
    client.search(MARINA, "my card 4111 1111")
    client.track(
        {"speaker": {"role": "customer"}, "handles": [MARINA], "content": {"text": "call +5511912345678"}}
    )
    client.flush()
    client.track(
        {"speaker": {"role": "nobody"}, "handles": [MARINA], "content": {"text": "call +5511912345678"}}
    )
    assert caplog.records
    assert "5511912345678" not in caplog.text
    assert "4111" not in caplog.text
