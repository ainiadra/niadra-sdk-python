"""The HTTP surface of the emulator, framework-free.

`MockApp.handle()` maps one request to one response. `MockApp.asgi` and `MockApp.wsgi`
expose it to `httpx.ASGITransport` and `httpx.WSGITransport`, so tests run the SDK
against the emulator in-process, and the `niadra-mock` command serves it over HTTP.

Errors follow the API: `application/problem+json` with a catalog `code`, and messages that
never echo request values.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

from pydantic import BaseModel, TypeAdapter, ValidationError

from niadra.models.common import ObjectRef
from niadra.models.context import ContextRequest, SearchRequest, TimelineRequest
from niadra.models.events import (
    MAX_BATCH_ITEMS,
    BatchItem,
    BatchResponse,
    FeedbackRequest,
    ItemError,
    MediaUploadRequest,
)
from niadra.models.tokens import SubjectTokenRequest
from niadra.tools import BUILTIN_DEFINITIONS
from niadra.vocabulary import Verification
from niadra_mock.cell import ItemNotFoundError, MockCell, UploadRejectedError

_BATCH_ITEM: TypeAdapter[Any] = TypeAdapter(BatchItem)
_OBJECTS = "/v1/objects/"
_UPLOADS = "/_mock/media/"

Headers = dict[str, str]
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
StartResponse = Callable[[str, list[tuple[str, str]]], Any]

_REASONS = {
    200: "OK",
    201: "Created",
    207: "Multi-Status",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    421: "Misdirected Request",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}
_CODES = {
    400: "invalid_input",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    421: "wrong_cell",
    422: "invalid_input",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}


@dataclass
class Response:
    status: int
    body: bytes
    headers: Headers = field(default_factory=dict)


def _json(status: int, payload: Any, headers: Headers | None = None) -> Response:
    return Response(
        status, json.dumps(payload).encode(), {"content-type": "application/json", **(headers or {})}
    )


def _problem(status: int, detail: str | None = None, headers: Headers | None = None) -> Response:
    code = _CODES.get(status, "error")
    body: dict[str, Any] = {
        "type": f"https://docs.niadra.com/errors/{code}",
        "title": code.replace("_", " "),
        "status": status,
        "code": code,
        "request_id": uuid.uuid4().hex,
    }
    if detail:
        body["detail"] = detail
    return Response(
        status, json.dumps(body).encode(), {"content-type": "application/problem+json", **(headers or {})}
    )


def _fields(error: ValidationError) -> str:
    # Field paths and error types only: echoing values could leak personal data.
    return "; ".join(".".join(map(str, e["loc"])) + ": " + e["type"] for e in error.errors())[:1000]


class MockApp:
    """Routes requests to a `MockCell`. Any `nia_sk_` key is accepted unless the cell revoked it."""

    def __init__(self, cell: MockCell | None = None) -> None:
        self.cell = cell or MockCell()

    def handle(
        self, method: str, path: str, query: str, headers: Headers, body: bytes, scheme: str = "http"
    ) -> Response:
        if path == "/healthz":
            return _json(200, {"status": "ok"})
        if path.startswith(_UPLOADS):
            # Storage, not the API: the signed URL is the credential, and a source key is refused.
            return self._receive_upload(method, path[len(_UPLOADS) :], headers, body)
        denied = self._authenticate(headers)
        if denied is not None:
            return denied
        failure = self.cell.take_failure(path)
        if failure is not None:
            extra = {"retry-after": str(failure.retry_after)} if failure.retry_after is not None else {}
            return _problem(failure.status, "injected by niadra-mock", extra)
        try:
            if path == "/v1/media/uploads" and method == "POST":
                return self._reserve_upload(body, f"{scheme}://{headers.get('host', 'localhost')}")
            return self._route(method, path, parse_qs(query), body)
        except ValidationError as exc:
            return _problem(422, _fields(exc))
        except ItemNotFoundError:
            return _problem(404)
        except ValueError:
            return _problem(400, "malformed request")

    def _authenticate(self, headers: Headers) -> Response | None:
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.startswith("nia_sk_"):
            return _problem(401, "missing or malformed source key")
        if token in self.cell.revoked_keys:
            return _problem(401, "the key was revoked")
        if token in self.cell.forbidden_keys:
            return _problem(403, "the source's access was cut")
        return None

    def _route(self, method: str, path: str, query: dict[str, list[str]], body: bytes) -> Response:
        routes: dict[tuple[str, str], Callable[[bytes], Response]] = {
            ("POST", "/v1/batch"): self._batch,
            ("POST", "/v1/context"): self._context,
            ("POST", "/v1/history/search"): self._search,
            ("POST", "/v1/history/timeline"): self._timeline,
            ("POST", "/v1/subject-tokens"): self._subject_token,
            ("POST", "/v1/feedback"): self._feedback,
        }
        if (method, path) in routes:
            return routes[(method, path)](body)
        if method == "GET" and path == "/v1/history/tools":
            return _json(200, {"tools": BUILTIN_DEFINITIONS})
        prefix = "/v1/history/items/"
        if method == "GET" and path.startswith(prefix) and len(path) > len(prefix):
            level = Verification(query.get("verification", ["V0"])[0])
            return _model(self.cell.open(path[len(prefix) :], level))
        if path.startswith(_OBJECTS):
            return self._object(method, path[len(_OBJECTS) :], query)
        if any(p == path for _, p in routes) or path.startswith(prefix):
            return _problem(405)
        return _problem(404)

    def _object(self, method: str, rest: str, query: dict[str, list[str]]) -> Response:
        parts = [unquote(part) for part in rest.split("/")]
        timeline = len(parts) == 4 and parts[3] == "timeline"
        if len(parts) not in (3, 4) or (len(parts) == 4 and not timeline) or not all(parts[:3]):
            return _problem(404)
        if method != "GET":
            return _problem(405)
        ref = ObjectRef(type=parts[0], namespace=parts[1], id=parts[2])
        if not timeline:
            return _model(self.cell.object_state(ref))
        limit = int(query.get("limit", ["20"])[0])
        if not 1 <= limit <= 100:
            return _problem(422, "limit: between 1 and 100")
        return _model(self.cell.object_timeline(ref, query.get("cursor", [None])[0], limit))

    def _feedback(self, body: bytes) -> Response:
        response = self.cell.feedback(FeedbackRequest.model_validate_json(body))
        return _json(200, response.model_dump(mode="json"))

    def _reserve_upload(self, body: bytes, base_url: str) -> Response:
        reserved = self.cell.reserve_upload(MediaUploadRequest.model_validate_json(body), base_url)
        return Response(201, reserved.model_dump_json().encode(), {"content-type": "application/json"})

    def _receive_upload(self, method: str, ref: str, headers: Headers, body: bytes) -> Response:
        if method != "PUT":
            return _problem(405)
        if "authorization" in headers:
            return _problem(400, "a signed upload URL takes no other credentials")
        try:
            self.cell.receive_upload(ref, headers, body)
        except ItemNotFoundError:
            return _problem(403, "the upload URL is unknown or expired")
        except UploadRejectedError as exc:
            return _problem(403, str(exc))
        return Response(200, b"")

    def _batch(self, body: bytes) -> Response:
        data = json.loads(body or b"null")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_ITEMS:
            return _problem(422, f"items: between 1 and {MAX_BATCH_ITEMS} items")
        accepted = duplicates = 0
        errors: list[ItemError] = []
        for index, raw in enumerate(items):
            try:
                item = _BATCH_ITEM.validate_python(raw)
            except ValidationError as exc:
                errors.append(ItemError(index=index, code="invalid_item", detail=_fields(exc)))
                continue
            if self.cell.accept(item):
                accepted += 1
            else:
                duplicates += 1
        response = BatchResponse(accepted=accepted, duplicates=duplicates, errors=errors)
        return _json(207 if errors else 200, response.model_dump(mode="json"))

    def _context(self, body: bytes) -> Response:
        return _model(self.cell.context(ContextRequest.model_validate_json(body)))

    def _search(self, body: bytes) -> Response:
        return _model(self.cell.search(SearchRequest.model_validate_json(body)))

    def _timeline(self, body: bytes) -> Response:
        return _model(self.cell.timeline(TimelineRequest.model_validate_json(body)))

    def _subject_token(self, body: bytes) -> Response:
        return _model(self.cell.subject_token(SubjectTokenRequest.model_validate_json(body)))

    async def asgi(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The ASGI application, for `httpx.ASGITransport` or any ASGI server."""
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        query = scope.get("query_string", b"").decode("latin-1")
        response = self.handle(
            scope["method"], scope["path"], query, headers, body, scope.get("scheme", "http")
        )
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": [(k.encode(), v.encode()) for k, v in response.headers.items()],
            }
        )
        await send({"type": "http.response.body", "body": response.body})

    def wsgi(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        """The WSGI application, for `httpx.WSGITransport` and the `niadra-mock` server."""
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        headers = {
            key[5:].replace("_", "-").lower(): value
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }
        if environ.get("CONTENT_TYPE"):
            headers["content-type"] = environ["CONTENT_TYPE"]
        response = self.handle(
            environ["REQUEST_METHOD"],
            environ.get("PATH_INFO", "/"),
            environ.get("QUERY_STRING", ""),
            headers,
            body,
            environ.get("wsgi.url_scheme", "http"),
        )
        status = f"{response.status} {_REASONS.get(response.status, 'Unknown')}"
        start_response(status, [*response.headers.items(), ("content-length", str(len(response.body)))])
        return [response.body]


def _model(model: BaseModel) -> Response:
    return _json(200, model.model_dump(mode="json", exclude_none=True))
