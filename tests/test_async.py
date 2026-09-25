from __future__ import annotations

import asyncio
import json

import httpx
import respx

from niadra import AsyncNiadra, current_session, email, phone
from niadra.options import CacheOptions, QueueOptions
from tests.conftest import BASE, KEY, batch_ok, context_payload

MARINA = phone("+5511912345678")


async def test_context_is_cached_per_conversation(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    async with AsyncNiadra(KEY, strict=True) as client:
        first = await client.context(MARINA, conversation_id="c-1")
        second = await client.context(MARINA, conversation_id="c-1")
    assert route.call_count == 1
    assert (first.origin, second.origin) == ("network", "cache")


async def test_stale_values_refresh_once_in_the_background(respx_mock: respx.MockRouter) -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=context_payload(etag=f"etag-{calls}"))

    respx_mock.post(f"{BASE}/v1/context").mock(side_effect=respond)
    async with AsyncNiadra(KEY, cache=CacheOptions(ttl=0, stale_while_revalidate=60)) as client:
        await client.context(MARINA, conversation_id="c-1")
        served = await asyncio.gather(*[client.context(MARINA, conversation_id="c-1") for _ in range(5)])
        assert {c.origin for c in served} == {"stale"}
        await asyncio.sleep(0.1)
        assert calls == 2
        assert (await client.context(MARINA, conversation_id="c-1")).etag == "etag-2"


async def test_auth_failure_purges_and_returns_empty(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").mock(
        side_effect=[httpx.Response(200, json=context_payload()), httpx.Response(403)]
    )
    async with AsyncNiadra(KEY, cache=CacheOptions(ttl=0, stale_while_revalidate=0)) as client:
        await client.context(MARINA, conversation_id="c-1")
        cut = await client.context(MARINA, conversation_id="c-1")
        assert not cut and len(client._cache) == 0


async def test_track_batches_on_a_background_task(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok(2))
    client = AsyncNiadra(KEY, channel="chat", queue=QueueOptions(batch_size=2, interval=3600))
    client.track({"speaker": {"role": "customer"}, "handles": [MARINA], "content": {"text": "a"}})
    client.track({"speaker": {"role": "customer"}, "handles": [MARINA], "content": {"text": "b"}})
    for _ in range(100):
        if route.called:
            break
        await asyncio.sleep(0.01)
    assert len(json.loads(route.calls.last.request.content)["items"]) == 2
    await client.close()


async def test_a_conversation_turn_wakes_the_background_task(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    queue = QueueOptions(batch_size=100, interval=3600, turn_interval=0.05)
    client = AsyncNiadra(KEY, channel="chat", queue=queue)
    client.track({"speaker": {"role": "customer"}, "handles": [MARINA], "content": {"text": "a"}})
    await asyncio.sleep(0.2)
    assert not route.called, "an item outside a conversation waits for the interval"
    client.track(
        {
            "conversation_id": "wa-1",
            "speaker": {"role": "customer"},
            "handles": [MARINA],
            "content": {"text": "b"},
        }
    )
    for _ in range(200):
        if route.called and client.pending == 0:
            break
        await asyncio.sleep(0.01)
    assert len(json.loads(route.calls.last.request.content)["items"]) == 2
    await client.close()


async def test_track_outside_a_loop_waits_for_flush(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    client = AsyncNiadra(KEY, channel="chat", queue=QueueOptions(batch_size=1, interval=3600))
    await asyncio.to_thread(
        client.track, {"speaker": {"role": "customer"}, "handles": [MARINA], "content": {"text": "a"}}
    )
    assert not route.called and client.pending == 1
    assert await client.flush()
    assert route.called
    await client.close()


async def test_identify_verify_and_subject_token(respx_mock: respx.MockRouter) -> None:
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    respx_mock.post(f"{BASE}/v1/subject-tokens").respond(
        200, json={"token": "st_1", "expires_at": "2026-09-22T14:22:00Z"}
    )
    async with AsyncNiadra(KEY, strict=True) as client:
        assert (await client.identify([MARINA, email("m@x.co")])) is not None
        assert (await client.verify("otp_sms", "V2", handle=MARINA, conversation_id="c-1")) is not None
        token = await client.subject_token(MARINA)
    assert batch.call_count == 2
    assert token is not None and token.token == "st_1"


async def test_navigation_and_tools(respx_mock: respx.MockRouter) -> None:
    item = {"id": "ep_1", "kind": "episode", "text": "missed visit", "at": "2026-09-22T14:02:00Z"}
    respx_mock.post(f"{BASE}/v1/history/search").respond(200, json={"items": [item], "tokens_used": 3})
    respx_mock.post(f"{BASE}/v1/history/timeline").respond(200, json={"items": [item]})
    opened_route = respx_mock.post(f"{BASE}/v1/history/open").respond(
        200, json={"id": "ep_1", "kind": "episode", "summary": "missed visit"}
    )
    async with AsyncNiadra(KEY, strict=True) as client:
        assert (await client.search(MARINA, "visit")).items[0].id == "ep_1"
        assert (await client.timeline(MARINA)).items[0].id == "ep_1"
        opened = await client.open("ep_1")
        assert opened is not None and opened.summary == "missed visit"
        kit = client.tools(MARINA)
        assert json.loads(await kit.call("get_customer_timeline", {"limit": 5}))["items"][0]["id"] == "ep_1"
        assert json.loads(await kit.call("open_history_item", {"id": "ep_1"}))["kind"] == "episode"
    sent = json.loads(opened_route.calls.last.request.content)
    assert sent == {
        "item_id": "ep_1",
        "subject": {"type": "phone_e164", "value": "+5511912345678"},
        "verification": "V0",
    }


async def test_async_conversation(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/context").respond(200, json=context_payload())
    batch = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    async with AsyncNiadra(KEY, channel="app", strict=True) as client:
        async with client.conversation("c-1", subject=MARINA) as conversation:
            assert current_session() is conversation
            assert await conversation.context()
            conversation.customer("hi")
            conversation.handoff("agent", target_source="billing-bot")
            conversation.action("lookup", object="order:shop:77")
        async with client.task("t-1", object="order:shop:77") as task:
            await task.context()
    items = [i for call in batch.calls for i in json.loads(call.request.content)["items"]]
    assert [i["type"] for i in items] == ["event", "handoff", "event", "conversation.ended", "task.ended"]
