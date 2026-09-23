"""Object reads, feedback and media uploads, against recorded routes and against the emulator."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
import respx

from niadra import AsyncNiadra, Niadra, phone
from niadra.errors import NotFoundError, PermissionDeniedError
from tests.conftest import BASE, KEY

MARINA = phone("+5511912345678")
STATE = {
    "ref": {"type": "invoice", "namespace": "erp", "id": "0823"},
    "state": {"status": "credited", "amount": "40.00"},
    "as_of": "2026-09-22T14:06:00Z",
    "source_id": "erp",
    "open_items": [],
}
TIMELINE = {
    "ref": STATE["ref"],
    "items": [{"id": "ev_2", "kind": "action", "text": "credit R$ 40", "at": "2026-09-22T14:06:00Z"}],
    "next_cursor": "c2",
}
UPLOAD_URL = "https://media.example-bucket.s3.amazonaws.com/sp/med_1?X-Amz-Signature=abc"


SIGNED = {"Content-Type": "audio/wav", "x-amz-checksum-sha256": "c2lnbmVk"}


def reserved(url: str = UPLOAD_URL, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"media_ref": "med_1", "upload_url": url, "expires_at": "2026-09-22T14:22:00Z"}
    if headers is not None:
        body["upload_headers"] = headers
    return body


def test_object_state_reads_the_object_path(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.get(f"{BASE}/v1/objects/invoice/erp/0823").respond(200, json=STATE)
    state = client.object_state("invoice:erp:0823")
    assert state is not None and state.state["status"] == "credited"
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"


def test_object_timeline_pages_and_encodes_ids(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.get(
        url__startswith=f"{BASE}/v1/objects/ticket/zendesk%20eu/A%3A1%23/timeline"
    ).respond(200, json={**TIMELINE, "ref": {"type": "ticket", "namespace": "zendesk eu", "id": "A:1#"}})
    page = client.object_timeline("ticket:zendesk eu:A:1#", cursor="c1", limit=5)
    assert page is not None and page.next_cursor == "c2" and page.items[0].kind == "action"
    assert dict(route.calls.last.request.url.params) == {"cursor": "c1", "limit": "5"}


def test_object_reads_fail_open(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    respx_mock.get(url__startswith=f"{BASE}/v1/objects/").respond(503)
    assert lenient.object_state("invoice:erp:0823") is None
    assert lenient.object_timeline("invoice:erp:0823") is None
    assert lenient.object_state("invoice:erp:08/23") is None, "a slash cannot be addressed"
    assert lenient.object_timeline("invoice:erp:0823", limit=500) is None


def test_feedback_sends_the_contract_body(respx_mock: respx.MockRouter, client: Niadra) -> None:
    route = respx_mock.post(f"{BASE}/v1/feedback").respond(
        200, json={"accepted": 1, "duplicates": 0, "errors": []}
    )
    result = client.feedback(
        "correct_fact",
        MARINA,
        fact_id="f-9",
        value="prefers e-mail",
        reason="told the agent",
        idempotency_key="fb-1",
    )
    assert result is not None and result.accepted == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "idempotency_key": "fb-1",
        "subject": {"type": "phone_e164", "value": "+5511912345678"},
        "action": "correct_fact",
        "fact_id": "f-9",
        "value": "prefers e-mail",
        "reason": "told the agent",
    }
    assert route.calls.last.request.headers["idempotency-key"] == "fb-1"


def test_feedback_fails_open_and_raises_in_strict(
    respx_mock: respx.MockRouter, lenient: Niadra, client: Niadra
) -> None:
    respx_mock.post(f"{BASE}/v1/feedback").respond(403)
    assert lenient.feedback("retract_fact", MARINA, fact_id="f-1") is None
    assert lenient.feedback("nonsense", MARINA) is None  # type: ignore[arg-type]
    with pytest.raises(PermissionDeniedError):
        client.feedback("retract_fact", MARINA, fact_id="f-1")


def test_upload_reserves_then_puts_the_bytes_without_the_key(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    data = b"RIFF....WAVEfmt "
    reserve = respx_mock.post(f"{BASE}/v1/media/uploads").respond(201, json=reserved(headers=SIGNED))
    put = respx_mock.put(UPLOAD_URL).mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    upload = client.upload_media(data, "audio/wav", subject=MARINA)
    assert upload is not None
    assert upload.media_ref == "med_1"
    assert upload.media_sha256 == hashlib.sha256(data).hexdigest()
    assert (upload.content_type, upload.size_bytes) == ("audio/wav", len(data))
    assert json.loads(reserve.calls.last.request.content) == {
        "content_type": "audio/wav",
        "size_bytes": len(data),
        "sha256": upload.media_sha256,
        "subject": {"type": "phone_e164", "value": "+5511912345678"},
    }
    assert put.call_count == 2, "storage errors are retried"
    request = put.calls.last.request
    assert request.content == data
    assert request.headers["content-type"] == "audio/wav"
    assert request.headers["x-amz-checksum-sha256"] == "c2lnbmVk"
    assert request.headers["content-length"] == str(len(data))
    assert "authorization" not in request.headers
    assert not request.headers.get("user-agent", "").startswith("niadra"), "nothing of ours goes along"


def test_upload_without_named_headers_sends_the_content_type(
    respx_mock: respx.MockRouter, client: Niadra
) -> None:
    respx_mock.post(f"{BASE}/v1/media/uploads").respond(201, json=reserved())
    put = respx_mock.put(UPLOAD_URL).respond(200)
    assert client.upload_media(b"png", "image/png") is not None
    request = put.calls.last.request
    assert request.headers["content-type"] == "image/png"
    assert "x-amz-checksum-sha256" not in request.headers


def test_upload_refuses_plain_http_storage(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    respx_mock.post(f"{BASE}/v1/media/uploads").respond(201, json=reserved("http://bucket.example.com/med_1"))
    put = respx_mock.put("http://bucket.example.com/med_1").respond(200)
    assert lenient.upload_media(b"bytes", "image/png") is None
    assert not put.called


def test_upload_without_a_url_skips_the_transfer(respx_mock: respx.MockRouter, client: Niadra) -> None:
    respx_mock.post(f"{BASE}/v1/media/uploads").respond(201, json=reserved(""))
    upload = client.upload_media(b"already there", "text/plain")
    assert upload is not None and upload.media_ref == "med_1"


def test_upload_fails_open(respx_mock: respx.MockRouter, lenient: Niadra) -> None:
    respx_mock.post(f"{BASE}/v1/media/uploads").respond(201, json=reserved())
    respx_mock.put(UPLOAD_URL).respond(403)
    assert lenient.upload_media(b"bytes", "image/png") is None
    assert lenient.upload_media(b"", "image/png") is None


def test_disabled_client_does_nothing() -> None:
    niadra = Niadra(api_key="")
    assert niadra.object_state("invoice:erp:1") is None
    assert niadra.object_timeline("invoice:erp:1") is None
    assert niadra.feedback("retract_fact", MARINA, fact_id="f") is None
    assert niadra.upload_media(b"x", "text/plain") is None


def test_objects_feedback_and_media_on_the_emulator(on_mock: Niadra, mock_app: Any) -> None:
    on_mock.track(
        {
            "kind": "system_event",
            "channel": "erp",
            "handles": [MARINA],
            "object_refs": ["invoice:erp:0823"],
            "speaker": {"role": "system"},
            "canonical_type": "invoice.credited",
            "fields": {"status": "credited"},
        }
    )
    on_mock.action("credit", subject=MARINA, object="invoice:erp:0823", result="R$ 40", channel="billing")
    on_mock.flush()

    state = on_mock.object_state("invoice:erp:0823")
    assert state is not None
    assert state.state == {"status": "credited", "last_event_type": "invoice.credited"}
    page = on_mock.object_timeline("invoice:erp:0823", limit=1)
    assert page is not None and len(page.items) == 1 and page.next_cursor == "1"
    assert page.items[0].kind == "action"

    first = on_mock.feedback("resolve_open_item", MARINA, open_item_id="oi-1", idempotency_key="fb-1")
    again = on_mock.feedback("resolve_open_item", MARINA, open_item_id="oi-1", idempotency_key="fb-1")
    assert first is not None and first.accepted == 1
    assert again is not None and again.duplicates == 1
    assert mock_app.cell.events[-1].item.canonical_type == "feedback.resolve_open_item"

    upload = on_mock.upload_media(b"\x89PNG....", "image/png")
    assert upload is not None
    assert mock_app.cell.media[upload.media_ref] == b"\x89PNG...."


async def test_the_async_client_has_the_same_calls(on_mock_async: AsyncNiadra, mock_app: Any) -> None:
    on_mock_async.action("credit", subject=MARINA, object="invoice:erp:0823", channel="billing")
    await on_mock_async.flush()
    assert await on_mock_async.object_state("invoice:erp:0823") is not None
    page = await on_mock_async.object_timeline("invoice:erp:0823")
    assert page is not None and [item.kind for item in page.items] == ["action"]
    with pytest.raises(NotFoundError):
        await on_mock_async.object_state("invoice:erp:missing")
    result = await on_mock_async.feedback(
        "conversation_outcome", MARINA, conversation_id="c-1", value="resolved"
    )
    assert result is not None and result.accepted == 1
    upload = await on_mock_async.upload_media(b"voice", "audio/ogg")
    assert upload is not None and mock_app.cell.media[upload.media_ref] == b"voice"
