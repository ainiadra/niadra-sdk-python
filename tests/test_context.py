from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from niadra import AuthenticationError, Context, Niadra, PermissionDeniedError, Verification, phone
from niadra._cache import ContextCache
from niadra.options import CacheOptions, Timeouts
from tests.conftest import BASE, KEY, context_payload

MARINA = phone("+5511912345678")
URL = f"{BASE}/v1/context"


def test_sends_the_contract_body_and_returns_a_typed_context(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    route = respx_mock.post(URL).respond(200, json=context_payload())
    context = client.context(
        MARINA,
        about={"type": "system_id", "scope": "crm", "value": "A-9"},
        view="voice",
        verification="V1",
        conversation_id="c-1",
        query="billing",
        target="openai/gpt-4.1",
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "subject": {"type": "phone_e164", "value": "+5511912345678"},
        "about": {"type": "system_id", "value": "A-9", "scope": "crm"},
        "view": "voice",
        "verification": "V1",
        "conversation_id": "c-1",
        "query": "billing",
        "delta": False,
        "target": {"provider": "openai", "model": "gpt-4.1"},
    }
    assert context.text and context.system_block.startswith("<context")
    assert context.variables == {"name": "Marina"}
    assert (context.etag, context.withheld, context.path) == ("etag-1", 2, "t0")
    assert context.cache is not None and context.cache.breakpoints == [12]
    assert context.verification.effective is Verification.V1
    assert context.origin == "network"
    assert context.elapsed_ms is not None


def test_object_targets_use_the_shorthand(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(URL).respond(200, json=context_payload())
    client.context(object="invoice:erp:0823", view="task:billing", task_id="t-1")
    body = json.loads(route.calls.last.request.content)
    assert body["object"] == {"type": "invoice", "namespace": "erp", "id": "0823"}
    assert body["task_id"] == "t-1"


def test_voice_views_get_the_shorter_budget(respx_mock: respx.MockRouter, client: Niadra) -> None:
    budgets: list[float] = []

    def record(request: httpx.Request) -> httpx.Response:
        budgets.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=context_payload())

    respx_mock.post(URL).mock(side_effect=record)
    client.context(MARINA, view="voice")
    client.context(MARINA, view="chat")
    assert budgets[0] == pytest.approx(0.15, abs=0.01)
    assert budgets[1] == pytest.approx(0.30, abs=0.01)


def test_live_turns_and_delta_form_the_turn_block(respx_mock: respx.MockRouter, client: Niadra) -> None:
    live = [
        {
            "at": "2026-09-22T14:02:00Z",
            "channel": "whatsapp",
            "kind": "message",
            "speaker": "customer",
            "text": "charged twice",
            "source_id": "s1",
        }
    ]
    respx_mock.post(URL).respond(
        200, json=context_payload(live=live, live_complete=False, delta="<delta>credit issued</delta>")
    )
    context = client.context(MARINA, delta=True)
    assert context.system_block == context_payload()["text"]
    assert context.turn_block == (
        "<delta>credit issued</delta>\n\n"
        '<live_turns source="niadra" complete="false">\n'
        "[2026-09-22T14:02:00Z] whatsapp · customer: charged twice\n"
        "</live_turns>"
    )


def test_unknown_response_fields_are_ignored(respx_mock: respx.MockRouter, client: Niadra) -> None:
    respx_mock.post(URL).respond(200, json=context_payload(brand_new_field={"x": 1}, path="t9"))
    context = client.context(MARINA)
    assert context.path == "t9"


def test_holdout_is_an_empty_pack_and_not_an_error(respx_mock: respx.MockRouter, client: Niadra) -> None:
    respx_mock.post(URL).respond(200, json=context_payload(text=None, path="holdout"))
    context = client.context(MARINA, conversation_id="c-1")
    assert context.is_holdout
    assert not context
    assert context.system_block == ""
    assert context.error is None


def test_without_a_conversation_nothing_is_cached(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(URL).respond(200, json=context_payload())
    client.context(MARINA)
    client.context(MARINA)
    assert route.call_count == 2


def test_fresh_entries_are_served_from_the_cache(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(URL).respond(200, json=context_payload())
    first = client.context(MARINA, conversation_id="c-1")
    second = client.context(MARINA, conversation_id="c-1")
    assert route.call_count == 1
    assert second.origin == "cache"
    assert second.text == first.text


def test_plain_and_delta_reads_share_the_conversation_entry(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    route = respx_mock.post(URL).respond(200, json=context_payload())
    client.context(MARINA, conversation_id="c-1")
    client.context(MARINA, conversation_id="c-1", delta=True)
    assert route.call_count == 1
    client.context(MARINA, conversation_id="c-1", use_cache=False)
    assert route.call_count == 2


def test_a_delta_is_handed_out_once(respx_mock: respx.MockRouter) -> None:
    niadra = Niadra(KEY, cache=CacheOptions(ttl=60.0, stale_while_revalidate=0.0))
    niadra._closed = True
    respx_mock.post(URL).respond(200, json=context_payload(delta="[New] credit of R$ 40"))
    first = niadra.context(MARINA, conversation_id="c-1", delta=True)
    again = niadra.context(MARINA, conversation_id="c-1", delta=True)
    assert first.delta == "[New] credit of R$ 40"
    assert again.origin == "cache"
    assert again.delta is None
    assert again.text == first.text


def test_a_delta_fetched_in_the_background_waits_for_the_next_read(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=context_payload()),
            httpx.Response(
                200,
                json=context_payload(not_modified=True, text=None, path="not_modified", delta="[New] refund"),
            ),
        ]
    )
    niadra = stale_client()
    niadra.context(MARINA, conversation_id="c-1")
    assert niadra.context(MARINA, conversation_id="c-1", delta=True).delta is None
    assert niadra._refresher is not None
    niadra._refresher.shutdown(wait=True)
    after = niadra.context(MARINA, conversation_id="c-1", delta=True)
    assert after.delta == "[New] refund"
    assert after.text == context_payload()["text"]


def test_a_new_pack_drops_deltas_pending_against_the_old_one() -> None:
    cache = ContextCache(CacheOptions())
    pack = Context.model_validate(context_payload())
    cache.absorb("k", "c:c-1", pack)
    refreshed = context_payload(not_modified=True, text=None, path="not_modified", delta="[New] refund")
    cache.absorb("k", "c:c-1", Context.model_validate(refreshed), deliver=False)
    repinned = Context.model_validate(context_payload(etag="etag-2", text="<context>recompiled</context>"))
    served = cache.absorb("k", "c:c-1", repinned)
    assert (served.etag, served.delta) == ("etag-2", None)


def stale_client() -> Niadra:
    niadra = Niadra(KEY, cache=CacheOptions(ttl=0.0, stale_while_revalidate=60.0))
    niadra._closed = True
    return niadra


def test_stale_entries_are_served_while_one_refresh_runs(respx_mock: respx.MockRouter) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) > 1:
            time.sleep(0.05)
        return httpx.Response(200, json=context_payload(etag=f"etag-{len(calls)}"))

    respx_mock.post(URL).mock(side_effect=respond)
    niadra = stale_client()
    niadra.context(MARINA, conversation_id="c-1")
    served = [niadra.context(MARINA, conversation_id="c-1") for _ in range(5)]
    assert {c.origin for c in served} == {"stale"}
    assert {c.etag for c in served} == {"etag-1"}
    deadline = time.monotonic() + 2
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    assert len(calls) == 2, "the background refresh is deduplicated by key"
    assert calls[1]["known_etag"] == "etag-1"
    assert niadra.context(MARINA, conversation_id="c-1").etag == "etag-2"
    assert niadra._refresher is not None
    niadra._refresher.shutdown(wait=True)


def test_not_modified_keeps_the_cached_pack(respx_mock: respx.MockRouter) -> None:
    niadra = Niadra(KEY, cache=CacheOptions(ttl=0.0, stale_while_revalidate=0.0))
    niadra._closed = True
    route = respx_mock.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=context_payload()),
            httpx.Response(200, json=context_payload(not_modified=True, text=None, path="not_modified")),
        ]
    )
    niadra.context(MARINA, conversation_id="c-1")
    again = niadra.context(MARINA, conversation_id="c-1")
    assert json.loads(route.calls.last.request.content)["known_etag"] == "etag-1"
    assert again.text == context_payload()["text"]
    assert again.origin == "cache"


def test_errors_fall_back_to_the_last_good_pack(respx_mock: respx.MockRouter) -> None:
    niadra = Niadra(KEY, cache=CacheOptions(ttl=0.0, stale_while_revalidate=0.0))
    niadra._closed = True
    respx_mock.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=context_payload()),
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
        ]
    )
    niadra.context(MARINA, conversation_id="c-1")
    fallback = niadra.context(MARINA, conversation_id="c-1")
    assert fallback.origin == "last_good"
    assert fallback.text == context_payload()["text"]


def test_degraded_answers_never_replace_a_good_pack(respx_mock: respx.MockRouter) -> None:
    niadra = Niadra(KEY, cache=CacheOptions(ttl=0.0, stale_while_revalidate=0.0))
    niadra._closed = True
    respx_mock.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=context_payload()),
            httpx.Response(200, json=context_payload(text=None, degraded=True, path="t4", etag="e0")),
        ]
    )
    niadra.context(MARINA, conversation_id="c-1")
    second = niadra.context(MARINA, conversation_id="c-1")
    assert second.origin == "last_good"
    assert second.etag == "etag-1"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_purge_the_cache_and_return_empty(respx_mock: respx.MockRouter, status: int) -> None:
    niadra = Niadra(KEY, cache=CacheOptions(ttl=0.0, stale_while_revalidate=0.0))
    niadra._closed = True
    respx_mock.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=context_payload()),
            httpx.Response(status),
            httpx.Response(200, json=context_payload(etag="etag-9")),
        ]
    )
    niadra.context(MARINA, conversation_id="c-1")
    cut = niadra.context(MARINA, conversation_id="c-1")
    assert not cut
    assert cut.origin == "empty"
    assert len(niadra._cache) == 0
    assert niadra.context(MARINA, conversation_id="c-1").etag == "etag-9"


def test_401_clears_every_conversation_and_403_only_its_key(respx_mock: respx.MockRouter) -> None:
    niadra = Niadra(KEY, strict=True, cache=CacheOptions(ttl=0.0, stale_while_revalidate=0.0))
    niadra._closed = True
    respx_mock.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=context_payload()),
            httpx.Response(200, json=context_payload()),
            httpx.Response(403),
        ]
    )
    niadra.context(MARINA, conversation_id="c-1")
    niadra.context(MARINA, conversation_id="c-2")
    with pytest.raises(PermissionDeniedError):
        niadra.context(MARINA, conversation_id="c-1")
    assert len(niadra._cache) == 1

    respx_mock.post(URL).respond(401)
    with pytest.raises(AuthenticationError):
        niadra.context(MARINA, conversation_id="c-2")
    assert len(niadra._cache) == 0


def test_a_timeout_returns_an_empty_context(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    niadra = Niadra(KEY, timeouts=Timeouts(context=0.05))
    niadra._closed = True
    started = time.monotonic()
    context = niadra.context(MARINA)
    assert time.monotonic() - started < 1
    assert not context
    assert context.error is not None
    assert context.degraded


def test_invalid_arguments_return_an_empty_context(lenient: Niadra) -> None:
    assert lenient.context().error == "invalid_arguments"
    assert lenient.context(MARINA, object="invoice:erp:1").error == "invalid_arguments"
    assert lenient.context(MARINA, view="nope").error == "invalid_arguments"
    assert lenient.context(MARINA, conversation_id="c", task_id="t").error == "invalid_arguments"


def test_revalidation_keeps_the_pack_but_takes_fresh_live_turns(respx_mock: respx.MockRouter) -> None:
    niadra = Niadra(KEY, cache=CacheOptions(ttl=0.0, stale_while_revalidate=0.0))
    niadra._closed = True
    live = [
        {
            "at": "2026-09-22T14:09:00Z",
            "channel": "voice",
            "kind": "message",
            "speaker": "customer",
            "text": "calling again",
            "source_id": "s",
        }
    ]
    respx_mock.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=context_payload()),
            httpx.Response(
                200,
                json=context_payload(
                    not_modified=True, text=None, live=live, withheld=3, path="not_modified", as_of=None
                ),
            ),
        ]
    )
    niadra.context(MARINA, conversation_id="c-1")
    again = niadra.context(MARINA, conversation_id="c-1")
    assert again.text == context_payload()["text"]
    assert again.as_of is not None, "as_of describes the pack, so it is kept"
    assert [t.text for t in again.live] == ["calling again"]
    assert (again.withheld, again.path) == (3, "not_modified")


def test_cache_defaults_match_the_other_sdk() -> None:
    options = CacheOptions()
    assert (options.ttl, options.stale_while_revalidate, options.max_stale, options.max_entries) == (
        10.0,
        600.0,
        1800.0,
        1000,
    )


def test_packs_past_max_stale_are_never_served(respx_mock: respx.MockRouter) -> None:
    niadra = Niadra(KEY, cache=CacheOptions(ttl=0.0, stale_while_revalidate=0.0, max_stale=60.0))
    niadra._closed = True
    respx_mock.post(URL).mock(side_effect=[httpx.Response(200, json=context_payload()), httpx.Response(503)])
    niadra.context(MARINA, conversation_id="c-1")
    for entry in niadra._cache._entries.values():
        entry.stored_at -= 61
    old = niadra.context(MARINA, conversation_id="c-1")
    assert (old.origin, old.text) == ("empty", None)
    assert len(niadra._cache) == 0
