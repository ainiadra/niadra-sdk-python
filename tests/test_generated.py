"""The route models and methods generated from the server's OpenAPI document (spec/openapi/cell.json), and
the turn records of the specification's examples read through them."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pydantic
import pytest
import respx

from niadra import AsyncNiadra, Niadra, NotAvailableError, NotFoundError, ServerError, phone
from niadra.errors import ConfigurationError
from niadra.models.coordination import EffectReserve
from niadra.models.state import SdkProfile, StateViewRequest
from niadra.models.turns import PromoteRequest, TurnCall, TurnRecord, TurnsRequest, TurnTokens
from tests.conftest import BASE, KEY, batch_ok

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "spec" / "examples" / "turn-record").glob("*.json"))
NOT_BUILT = {
    "type": "about:blank",
    "title": "Not Implemented",
    "status": 501,
    "code": "not_implemented",
    "detail": "this route is declared and not built yet",
}


def _sync_spec() -> ModuleType:
    """The generator (scripts/sync_spec.py), which is a script, not part of the package."""
    if "sync_spec" not in sys.modules:
        spec = importlib.util.spec_from_file_location("sync_spec", ROOT / "scripts" / "sync_spec.py")
        assert spec is not None and spec.loader is not None
        sys.modules["sync_spec"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules["sync_spec"])
    return sys.modules["sync_spec"]


def _record(**fields: Any) -> TurnRecord:
    return TurnRecord.model_validate(
        {
            "turn_id": "t-1",
            "conversation_id": "c-8812",
            "agent": {"name": "closing"},
            "started_at": "2026-09-29T14:02:11Z",
            "content_mode": "hash_only",
            **fields,
        }
    )


def test_the_generated_files_are_what_the_cut_document_gives() -> None:
    sync = _sync_spec()
    for path, text in sync.generate(json.loads(sync.CUT.read_text())).items():
        assert (ROOT / path).read_text() == text, f"{path} is stale: run scripts/sync_spec.py"


def test_every_operation_of_the_document_is_a_method_of_both_clients() -> None:
    sync = _sync_spec()
    names = {op.name for op in sync.operations(json.loads(sync.CUT.read_text()))}
    assert len(names) == 59
    client = Niadra(KEY)
    assert all(callable(getattr(client.api, name)) for name in names)
    assert all(callable(getattr(AsyncNiadra(KEY).api, name)) for name in names)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_the_specifications_turn_records_read_and_write_back(path: Path) -> None:
    example = json.loads(path.read_text())
    record = TurnRecord.model_validate(example)
    written = record.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert written.keys() == example.keys()
    assert TurnRecord.model_validate(written) == record


def test_what_the_sdk_sends_refuses_unknown_fields_and_what_it_receives_ignores_them() -> None:
    with pytest.raises(pydantic.ValidationError):
        _record(surprise=1)
    profile = SdkProfile.model_validate({"features": ["turns"], "valid_for_s": 600, "later": True})
    assert profile.features == ["turns"]


def test_a_route_the_server_has_not_built_raises_not_available(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/v1/turns").respond(501, json=NOT_BUILT)
    lenient = Niadra(KEY)  # fail-open for the rest of the SDK; the routes still raise
    with pytest.raises(NotAvailableError, match="not available on this server yet") as raised:
        lenient.api.record_turns(TurnsRequest(turns=[_record()]))
    assert isinstance(raised.value, ServerError)
    assert raised.value.code == "not_implemented"
    assert respx_mock.calls.call_count == 1  # a 501 is final, never retried


async def test_the_async_routes_raise_the_same(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/v1/sdk/profile").respond(501, json=NOT_BUILT)
    respx_mock.post(f"{BASE}/v1/state/view").respond(404, json={"code": "not_found", "status": 404})
    async with AsyncNiadra(KEY) as client:
        with pytest.raises(NotAvailableError):
            await client.api.sdk_profile()
        # A feature the space did not turn on answers as a route that does not exist.
        with pytest.raises(NotFoundError):
            await client.api.state_view(StateViewRequest(subject=phone("+5511912345678")))


def test_a_body_travels_by_its_wire_names_and_only_with_what_was_set(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/turns").respond(207, json=batch_ok(0))
    call = TurnCall(call_id="m1", kind="model", tokens=TurnTokens(input=2953, output=138))
    answer = Niadra(KEY, strict=True).api.record_turns(TurnsRequest(turns=[_record(calls=[call])]))
    assert answer.accepted == 0
    sent = json.loads(route.calls.last.request.content)["turns"][0]
    assert sent["calls"] == [{"call_id": "m1", "kind": "model", "tokens": {"in": 2953, "out": 138}}]
    assert "fidelity" not in sent and "spec" not in sent  # defaults the caller left are the server's


def test_a_success_without_a_body_is_none_where_the_route_allows_one(respx_mock: respx.MockRouter) -> None:
    effect_id = "E" * 43  # a keyed hash of the effect key, base64url
    effect = {
        "effect_id": effect_id,
        "attempt": 1,
        "reserved_at": "2026-09-29T10:00:00Z",
        "state": "reserved",
    }
    respx_mock.post(f"{BASE}/v1/coordination/effects").mock(
        side_effect=[httpx.Response(201, json=effect), httpx.Response(200)]
    )
    body = EffectReserve(effect_key="farewell:c-1", kind="notice")
    reserved = Niadra(KEY).api.reserve_effect(body)
    assert reserved is not None and reserved.effect_id == effect_id
    assert Niadra(KEY).api.reserve_effect(body) is None


def test_idempotent_routes_carry_a_key(respx_mock: respx.MockRouter) -> None:
    body = {"promoted": 1, "already_kept": 0, "not_found": 0}
    route = respx_mock.post(f"{BASE}/v1/turns/promote").respond(200, json=body)
    client = Niadra(KEY)
    request = PromoteRequest(turn_ids=["t-1"], reason="complaint")
    client.api.promote_turns(request)
    client.api.promote_turns(request, idempotency_key="promote-t-1")
    first, second = (call.request.headers.get("idempotency-key") for call in route.calls)
    assert first and len(first) == 36
    assert second == "promote-t-1"


def test_path_and_query_parameters(respx_mock: respx.MockRouter) -> None:
    listed = respx_mock.get(url__startswith=f"{BASE}/v1/profiles/p_1%2Fx/inferences").respond(
        200, json={"items": []}
    )
    report = respx_mock.get(url__startswith=f"{BASE}/v1/measure/attribution").respond(
        200, json={"since": "2026-09-01", "until": "2026-09-28", "rows": []}
    )
    deleted = respx_mock.delete(f"{BASE}/v1/profiles/p_1/inferences/k%3A1").respond(204)
    client = Niadra(KEY)
    assert client.api.list_inferences("p_1/x", cursor="c2").items == []
    assert dict(listed.calls.last.request.url.params) == {"cursor": "c2", "limit": "50"}
    client.api.attribution(since=date(2026, 9, 1), until=date(2026, 9, 28))
    assert dict(report.calls.last.request.url.params) == {"since": "2026-09-01", "until": "2026-09-28"}
    client.api.delete_inference("p_1", "k:1")
    assert deleted.called


def test_a_client_without_a_key_raises_instead_of_sending() -> None:
    with pytest.raises(ConfigurationError):
        Niadra(api_key="").api.sdk_profile()


@pytest.mark.parametrize(
    "text",
    [
        "One entry of the turn record (front A5).",
        "The features of the agent core wave.",
        "Built in phase 2.",
        "As study 23 says.",
    ],
)
def test_a_server_description_naming_internal_planning_stops_the_generator(text: str) -> None:
    with pytest.raises(ValueError, match="internal planning"):
        _sync_spec().public(text, "Schema.field")


def test_a_body_the_server_declares_inline_is_published_as_its_named_schema() -> None:
    sync = _sync_spec()
    body = {"$id": "u", "title": "ThingRequest", "type": "object", "properties": {"n": {"type": "string"}}}
    document = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/v1/things": {
                "post": {
                    "tags": ["turns"],
                    "operationId": "record_things_v1_things_post",
                    "requestBody": {"content": {"application/json": {"schema": body}}},
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {"schemas": {}},
    }
    cut = sync.cut(document)
    schema = cut["paths"]["/v1/things"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ThingRequest"}
    assert cut["components"]["schemas"]["ThingRequest"] == {k: v for k, v in body.items() if k != "$id"}


def test_a_field_that_would_hide_a_type_of_its_class_stops_the_generator() -> None:
    sync = _sync_spec()
    schemas = {
        "Stamp": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "format": "date"},
                "since": {"type": "string", "format": "date"},
            },
        }
    }
    with pytest.raises(ValueError, match="hides a type"):
        sync.ModelWriter(schemas, {"Stamp": "niadra.models.turns"}, set(), "niadra.models.turns").definition(
            "Stamp"
        )
