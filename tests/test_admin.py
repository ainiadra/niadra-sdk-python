"""Search options, batch feedback, the key's identity and the governance calls of `niadra.admin`."""

from __future__ import annotations

import json

import respx

from niadra import AsyncNiadra, Niadra, email, phone
from tests.conftest import BASE, KEY, batch_ok

MARINA = phone("+5511912345678")
PROFILE = "0192f5a0-0000-7000-8000-000000000001"
FACT = "0192f5a0-0000-7000-8000-0000000000f1"
FACT_OUT = {
    "id": FACT,
    "predicate": "preferred_name",
    "value": "Marina",
    "category": "contact",
    "sensitivity": "normal",
    "status": "active",
    "confidence": 0.9,
    "valid_at": "2026-09-01T10:00:00Z",
    "last_confirmed_at": "2026-09-01T10:00:00Z",
    "times_seen": 2,
}
ERASURE = {
    "request_id": "er-1",
    "status": "pending",
    "target_kind": "profile",
    "target_id": PROFILE,
    "requested_at": "2026-09-25T10:00:00Z",
}


def test_search_sends_where_and_limit(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/history/search").respond(200, json={"items": [], "tokens_used": 0})
    where = {"AND": [{"kind": ["episode", "action"]}, {"NOT": {"vendor": "acme"}}]}
    client.search(MARINA, "segunda via", filters={"where": where}, limit=5)
    sent = json.loads(route.calls.last.request.content)
    assert sent["filters"]["where"] == where
    assert sent["limit"] == 5


def test_feedback_batch_mints_missing_keys(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/feedback/batch").respond(200, json=batch_ok(2))
    result = client.feedback_batch(
        [
            {"action": "retract_fact", "subject": MARINA, "fact_id": "f-1", "idempotency_key": "k1"},
            {"action": "correct_fact", "subject": email("ana@example.com"), "fact_id": "f-2", "value": "Ana"},
        ]
    )
    assert result is not None
    assert result.accepted == 2
    items = json.loads(route.calls.last.request.content)["items"]
    assert items[0]["idempotency_key"] == "k1"
    assert items[1]["idempotency_key"]
    assert items[1]["subject"]["value"] == "ana@example.com"


def test_whoami_reads_the_key(respx_mock: respx.MockRouter, client: Niadra) -> None:
    respx_mock.get(f"{BASE}/v1/sources/me").respond(
        200,
        json={
            "space_id": "s1",
            "tenant_id": "t1",
            "region": "us-east-2",
            "environment": "sandbox",
            "source_id": "src-1",
            "source_name": "whatsapp-bot",
            "vendor": "acme",
            "key_id": "k1",
            "scopes": ["context", "track"],
            "audience": "customer_agent",
            "verification_ceiling": "V2",
            "purposes": ["support"],
            "agent_memory": True,
            "a_field_from_a_later_release": 1,
        },
    )
    me = client.whoami()
    assert me is not None
    assert (me.source_name, me.scopes, me.agent_memory) == ("whatsapp-bot", ["context", "track"], True)


def test_admin_reads_memory_and_a_facts_history(respx_mock: respx.MockRouter, client: Niadra) -> None:
    respx_mock.post(f"{BASE}/v1/profiles/search").respond(
        200, json={"items": [{"profile_id": PROFILE, "pseudonym": "p_1", "kind": "person", "handles": []}]}
    )
    respx_mock.get(f"{BASE}/v1/profiles/{PROFILE}/memory").respond(
        200, json={"profile_id": PROFILE, "policy_version": "v3", "audience": "human", "facts": [FACT_OUT]}
    )
    history = respx_mock.get(f"{BASE}/v1/profiles/{PROFILE}/facts/{FACT}/history").respond(
        200,
        json={
            "profile_id": PROFILE,
            "fact_id": FACT,
            "predicate": "preferred_name",
            "policy_version": "v3",
            "versions": [FACT_OUT],
            "relations": [{"from_fact_id": FACT, "to_fact_id": "old", "type": "supersedes"}],
        },
    )
    [found] = client.admin.find_profiles("+5511912345678")
    memory = client.admin.memory(found.profile_id)
    assert memory is not None
    assert memory.facts[0].value == "Marina"
    chain = client.admin.fact_history(PROFILE, f"fact:{FACT}")
    assert chain is not None
    assert chain.relations[0].type == "supersedes"
    assert history.called


def test_admin_corrects_forgets_and_exports_with_idempotency_keys(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    one = respx_mock.post(f"{BASE}/v1/corrections").respond(202, json=batch_ok())
    many = respx_mock.post(f"{BASE}/v1/corrections/batch").respond(202, json=batch_ok(2))
    forget = respx_mock.post(f"{BASE}/v1/forget").respond(202, json=ERASURE)
    respx_mock.get(f"{BASE}/v1/forget/er-1").respond(200, json={**ERASURE, "status": "completed"})
    respx_mock.post(f"{BASE}/v1/export").respond(
        201,
        json={
            "run_id": "r1",
            "profile_id": PROFILE,
            "download_url": "https://bucket.example/r1.zip",
            "download_expires_at": "2026-09-26T10:00:00Z",
            "sha256": "ab",
        },
    )

    assert client.admin.correct(PROFILE, "correct_fact", fact_id=f"fact:{FACT}", value="Mari") is not None
    sent = one.calls.last.request
    assert json.loads(sent.content)["fact_id"] == FACT
    assert sent.headers["idempotency-key"]
    batch = client.admin.correct_batch(
        [{"profile_id": PROFILE, "action": "retract_fact", "fact_id": FACT}] * 2, idempotency_key="b1"
    )
    assert batch is not None
    assert many.calls.last.request.headers["idempotency-key"] == "b1"
    erasure = client.admin.forget(profile_id=PROFILE, idempotency_key="e1")
    assert erasure is not None
    assert json.loads(forget.calls.last.request.content) == {"target": "profile", "profile_id": PROFILE}
    status = client.admin.forget_status(erasure.request_id)
    assert status is not None
    assert status.status == "completed"
    package = client.admin.export(handle=MARINA)
    assert package is not None
    assert package.run_id == "r1"


def test_admin_fails_open_and_refuses_an_ambiguous_target(
    respx_mock: respx.MockRouter, lenient: Niadra
) -> None:
    respx_mock.get(url__startswith=f"{BASE}/v1/profiles/").respond(403, json={"code": "scope_missing"})
    assert lenient.admin.memory(PROFILE) is None
    assert lenient.admin.forget(profile_id=PROFILE, handle=MARINA) is None
    assert lenient.admin.export() is None
    assert Niadra(api_key=None).admin.find_profiles("p_1") == []


async def test_the_async_client_has_the_same_calls(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/v1/profiles/{PROFILE}/memory").respond(
        200, json={"profile_id": PROFILE, "policy_version": "v3", "audience": "human"}
    )
    respx_mock.post(f"{BASE}/v1/feedback/batch").respond(200, json=batch_ok())
    async with AsyncNiadra(KEY, strict=True) as niadra:
        memory = await niadra.admin.memory(PROFILE)
        batch = await niadra.feedback_batch([{"action": "retract_fact", "subject": MARINA, "fact_id": "f"}])
    assert memory is not None
    assert memory.facts == []
    assert batch is not None


def test_ingest_status_sends_the_thread_in_the_body(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/ingest/status").respond(
        200, json={"state": "ready", "extraction": "ok", "closed_at": "2026-09-25T10:00:00Z"}
    )
    status = client.ingest_status(conversation_id="wa-81")
    assert status is not None
    assert (status.state, status.extraction) == ("ready", "ok")
    assert json.loads(route.calls.last.request.content) == {"conversation_id": "wa-81"}
    assert "wa-81" not in str(route.calls.last.request.url)


def test_ingest_status_needs_exactly_one_thread(lenient: Niadra) -> None:
    assert lenient.ingest_status() is None
    assert lenient.ingest_status(conversation_id="c", task_id="t") is None
