import json
from datetime import UTC, datetime

import httpx
import pytest
from niadra.models.events import BatchItem
from pydantic import TypeAdapter

from niadra_bench.identity import Identities
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
    assert target.seed_report() == {"refused_actions": []}


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
