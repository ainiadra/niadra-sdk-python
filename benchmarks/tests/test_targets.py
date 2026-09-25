import json
from datetime import UTC, datetime

import httpx
import pytest
from niadra.models.events import BatchItem
from pydantic import TypeAdapter

from niadra_bench import sources
from niadra_bench.identity import Identities
from niadra_bench.sources import ControlPlane, dataset_operations, source_name
from niadra_bench.targets.mem0 import add_payloads, exchanges, render
from niadra_bench.targets.niadra import Keys, NiadraTarget, session_items

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
ITEM = TypeAdapter(BatchItem)


def test_niadra_items_are_valid_batch_items_in_order(cases) -> None:
    for case in cases[::17]:
        ids = Identities.for_case(case, "t1")
        for session in case.chronological():
            items = session_items(case, ids, session, NOW)
            for item in items:
                ITEM.validate_python(item)
            if session.turns:
                assert items[-1]["type"] == "conversation.ended"
                stamps = [i["occurred_at"] for i in items if i.get("kind") == "message"]
                assert stamps == sorted(stamps)


def test_sensitive_sessions_carry_the_verification_hint(cases) -> None:
    case = next(c for c in cases if c.category == "privacy")
    ids = Identities.for_case(case, "t1")
    session = next(s for s in case.sessions if s.sensitive)
    messages = [i for i in session_items(case, ids, session, NOW) if i.get("kind") == "message"]
    assert messages and all(m["verification_hint"] == "V2" for m in messages)


def test_the_crm_profile_names_every_handle(cases) -> None:
    case = cases[0]
    ids = Identities.for_case(case, "t1")
    profile = next(s for s in case.sessions if s.role == "profile")
    (event,) = session_items(case, ids, profile, NOW)
    assert {h["type"] for h in event["handles"]} == {"phone_e164", "email", "system_id", "app_user_id"}


def test_mem0_adds_one_call_per_exchange_and_system_records_raw(cases) -> None:
    case = next(c for c in cases if c.category == "promise_action")
    ids = Identities.for_case(case, "t1")
    voice = next(s for s in case.sessions if s.channel == "voice" and s.role == "key")
    assert [len(g) for g in exchanges(voice)] == [2, 1]
    erp = next(s for s in case.sessions if s.record and s.record.kind == "system_event" and s.role == "key")
    (payload,) = add_payloads(case, ids, "known_id", erp)
    assert payload["infer"] is False and payload["user_id"] == ids.mem0_user("known_id", "erp")
    per_channel = add_payloads(case, ids, "per_channel_id", voice)
    assert {p["user_id"] for p in per_channel} == {f"phone:{ids.phone}"}


def test_render_uses_the_mem0_documentation_format() -> None:
    header = "Based on previous conversations, I recall:"
    assert (
        render(header, [{"memory": "User needs it by the 17th"}, {"memory": ""}])
        == f"{header}\n- User needs it by the 17th"
    )
    assert render(header, []) == ""


def test_keys_fall_back_to_the_whatsapp_source(tmp_path) -> None:
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text('{"keys": {"whatsapp": "k-wa", "voice": "k-voice", "billing": "k-bill"}}')
    keys = Keys.from_env({"NIADRA_BOOTSTRAP": str(bootstrap)})
    assert keys.for_channel("voice") == ("voice", "k-voice")
    assert keys.for_channel("erp") == ("billing", "k-bill")
    assert keys.for_channel("email") == ("whatsapp", "k-wa")


async def test_an_operation_the_source_did_not_declare_is_counted_and_the_history_goes_on(cases) -> None:
    case = next(c for c in cases if c.category == "promise_action")
    ids = Identities.for_case(case, "t1")
    posted: list[list[dict]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        items = json.loads(request.content)["items"]
        posted.append(items)
        errors = [
            {
                "index": i,
                "code": "operation_not_allowed",
                "detail": "the source may not record this operation",
            }
            for i, item in enumerate(items)
            if item.get("kind") == "action"
        ]
        return httpx.Response(
            207 if errors else 200, json={"accepted": len(items) - len(errors), "errors": errors}
        )

    target = NiadraTarget(
        Keys({"whatsapp": "k-wa", "voice": "k-voice", "billing": "k-bill"}),
        base_url="http://niadra.test",
        transport_factory=lambda: httpx.MockTransport(answer),
        now=lambda: NOW,
    )
    await target.start()
    await target.seed(case, ids)
    await target.close()
    assert len(posted) == len(case.sessions)
    report = target.seed_report()
    assert len(report["refused_actions"]) == 1 and report["refused_actions"][0].endswith(case_operation(case))
    assert target.seed_report() == {"refused_actions": [], "declared_operations": None}


async def test_other_rejections_still_fail_the_case(cases) -> None:
    case = cases[0]
    ids = Identities.for_case(case, "t1")

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(207, json={"errors": [{"index": 0, "code": "invalid_input", "detail": "no"}]})

    target = NiadraTarget(
        Keys({"whatsapp": "k-wa"}),
        base_url="http://niadra.test",
        transport_factory=lambda: httpx.MockTransport(answer),
        now=lambda: NOW,
    )
    await target.start()
    with pytest.raises(RuntimeError, match="invalid_input"):
        await target.seed(case, ids)
    await target.close()


def case_operation(case) -> str:
    return next(s.record.operation for s in case.sessions if s.record and s.record.kind == "action")


def test_the_billing_source_declares_the_operations_the_dataset_records(cases) -> None:
    operations = dataset_operations(cases)
    assert operations == ["credit", "redeliver", "refund", "refund_fee", "reimburse"]
    assert source_name(operations) == source_name(reversed(operations))


async def test_the_billing_agent_gets_its_own_source_and_key_for_the_run(cases, monkeypatch) -> None:
    """As a customer sets it up: a source declaring the agent's operations, created once through the
    control API with the sandbox's admin, a key per run that the cell serves before seeding starts, and
    revoked at the end."""
    monkeypatch.setattr(sources.asyncio, "sleep", _no_wait)
    case = next(c for c in cases if c.category == "promise_action")
    calls: list[tuple[str, str, str | None]] = []
    served = iter([401, 200])

    def answer(request: httpx.Request) -> httpx.Response:
        path, auth = request.url.path, request.headers.get("authorization")
        calls.append((request.method, path, auth))
        if path == "/v1/auth/login":
            body = json.loads(request.content)
            assert body["totp"].isdigit() and body["space_id"] == "space-1"
            return httpx.Response(200, json={"access_token": "person"})
        if path == "/v1/sources" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/v1/sources":
            body = json.loads(request.content)
            assert body["trusted_action_ops"] == dataset_operations(cases)
            assert (body["audience"], body["channel"], body["purposes"]) == (
                "internal_agent",
                "erp",
                ["billing"],
            )
            return httpx.Response(201, json={"source_id": "src-9", **body})
        if path == "/v1/sources/src-9/keys":
            return httpx.Response(201, json={"key": {"key_id": "key-9"}, "secret": "k-bench-billing"})
        if path == "/v1/sources/src-9/keys/key-9/revoke":
            return httpx.Response(200, json={})
        if path == "/v1/context":
            return httpx.Response(next(served), json={})
        return httpx.Response(200, json={"accepted": len(json.loads(request.content)["items"]), "errors": []})

    document = {
        "admin_email": "a@b.c",
        "password": "p",
        "space_id": "space-1",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    }
    target = NiadraTarget(
        Keys({"whatsapp": "k-wa", "voice": "k-voice", "billing": "k-bill"}, document),
        base_url="http://niadra.test",
        transport_factory=lambda: httpx.MockTransport(answer),
        now=lambda: NOW,
        control_url=ControlPlane.available(document, {"NIADRA_CONTROL_URL": "http://control.test"}),
        operations=dataset_operations(cases),
    )
    await target.start()
    await target.seed(case, Identities.for_case(case, "t1"))
    assert target.seed_report() == {"refused_actions": [], "declared_operations": dataset_operations(cases)}
    await target.close()
    batches = [auth for method, path, auth in calls if path == "/v1/batch"]
    erp = [s for s in case.chronological() if s.channel in ("erp", "crm")]
    assert batches.count("Bearer k-bench-billing") == len(erp)
    assert "Bearer k-bill" not in batches
    assert calls[-1][1] == "/v1/sources/src-9/keys/key-9/revoke"


def test_without_the_admin_account_or_the_control_plane_nothing_is_provisioned() -> None:
    document = {"keys": {"whatsapp": "k"}}
    assert ControlPlane.available(document, {"NIADRA_CONTROL_URL": "http://control.test"}) is None
    full = {"admin_email": "a@b.c", "password": "p", "space_id": "s"}
    assert ControlPlane.available(full, {}) is None


async def _no_wait(seconds: float) -> None:
    return None
