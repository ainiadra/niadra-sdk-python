"""The HTTP surface of the emulator, framework-free.

`MockApp.handle()` maps one request to one response. `MockApp.asgi` and `MockApp.wsgi`
expose it to `httpx.ASGITransport` and `httpx.WSGITransport`, so tests run the SDK
against the emulator in-process, and the `niadra-mock` command serves it over HTTP.

Errors follow the API: `application/problem+json` with a catalog `code`, and messages that
never echo request values.
"""

from __future__ import annotations

import gzip
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

from pydantic import BaseModel, TypeAdapter, ValidationError

from niadra.models.agent_memory import (
    AgentMemorySearchRequest,
    CreateAgentNoteRequest,
    DistillRequest,
    UpdateAgentNoteRequest,
)
from niadra.models.common import ObjectRef
from niadra.models.context import (
    ContextRequest,
    OpenItemRequest,
    PrefetchRequest,
    SearchRequest,
    TimelineRequest,
)
from niadra.models.coordination import CheckBatchRequest, CheckRequest, ClaimRequest
from niadra.models.events import (
    MAX_BATCH_ITEMS,
    BatchItem,
    BatchResponse,
    FeedbackRequest,
    ItemError,
    MediaUploadRequest,
)
from niadra.models.state import AgentStateWrite
from niadra.models.tokens import SubjectTokenRequest
from niadra.models.turns import PromoteRequest
from niadra.tools import definitions
from niadra.turns.sender import MAX_TURNS
from niadra.vocabulary import Verification
from niadra_mock.agent_memory import MOCK_SOURCE, PersonalDataError
from niadra_mock.cell import ItemNotFoundError, MockCell, UploadRejectedError
from niadra_mock.coordinate import CoordinationError
from niadra_mock.features import FeatureOffError
from niadra_mock.replay import ReplayError
from niadra_mock.state import StateError

_BATCH_ITEM: TypeAdapter[Any] = TypeAdapter(BatchItem)
_OBJECTS = "/v1/objects/"
_AGENT_MEMORY = "/v1/agent-memory/"
_JSON = {"content-type": "application/json"}
_UPLOADS = "/_mock/media/"

Headers = dict[str, str]
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
StartResponse = Callable[[str, list[tuple[str, str]]], Any]

_REASONS = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    207: "Multi-Status",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    410: "Gone",
    412: "Precondition Failed",
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
    409: "conflict",
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


def _problem(
    status: int, detail: str | None = None, headers: Headers | None = None, *, code: str | None = None
) -> Response:
    code = code or _CODES.get(status, "error")
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


def _coded(status: int, code: str, extra: dict[str, Any]) -> Response:
    """A problem with a catalog code and the members the spec adds to it (`pins`, `reasons`)."""
    response = _problem(status, code=code)
    return Response(status, json.dumps({**json.loads(response.body), **extra}).encode(), response.headers)


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
        public = method == "GET" and path == "/.well-known/niadra-contact-keys.json"
        denied = None if public else self._authenticate(headers)
        if denied is not None:
            return denied
        failure = self.cell.take_failure(path)
        if failure is not None:
            extra = {"retry-after": str(failure.retry_after)} if failure.retry_after is not None else {}
            return _problem(failure.status, "injected by niadra-mock", extra)
        if public:
            if "coordination" not in self.cell.features:
                return _problem(404)
            return _json(200, self.cell.coordination.keys(parse_qs(query).get("space", [None])[0]))
        try:
            if path == "/v1/media/uploads" and method == "POST":
                return self._reserve_upload(body, f"{scheme}://{headers.get('host', 'localhost')}")
            if path == "/v1/turns" or path.startswith("/v1/turns/"):
                return self._turns(method, path, headers, body)
            if method == "GET" and path == "/v1/sdk/profile":
                return _model(self.cell.agent_features.profile())
            if method == "GET" and path == "/v1/suppressions":
                return _model(self.cell.agent_features.suppression_page())
            if method == "GET" and path == "/v1/suppressions/salt":
                return _model(self.cell.agent_features.suppression_salt())
            if method == "POST" and path == "/v1/types/fingerprint":
                return _json(200, self.cell.agent_features.type_fingerprint(json.loads(body)))
            answer = self._agent_features(method, path, parse_qs(query), body)
            if answer is not None:
                return answer
            if path.startswith(_AGENT_MEMORY):
                return self._agent_memory(method, path[len(_AGENT_MEMORY) :], parse_qs(query), headers, body)
            if method == "GET" and path == "/v1/history/tools":
                return self._tools(parse_qs(query), headers)
            return self._route(method, path, parse_qs(query), body)
        except ValidationError as exc:
            return _problem(422, _fields(exc))
        except (CoordinationError, StateError, ReplayError) as exc:
            return _coded(exc.status, exc.code, getattr(exc, "extra", {}))
        except (ItemNotFoundError, FeatureOffError):
            return _problem(404)
        except ValueError:
            return _problem(400, "malformed request")

    def _agent_features(
        self, method: str, path: str, query: dict[str, list[str]], body: bytes
    ) -> Response | None:
        """Coordination, working state, verify and refresh, and replay, each behind its feature."""
        cell = self.cell
        route = (method, path)
        if path.startswith("/v1/coordination/"):
            cell.agent_features.need("coordination")
            if route == ("POST", "/v1/coordination/check"):
                return _json(200, self._check(CheckRequest.model_validate_json(body)))
            if route == ("POST", "/v1/coordination/check/batch"):
                checks = CheckBatchRequest.model_validate_json(body).checks
                return _json(200, {"results": [self._check(c) for c in checks]})
            if route == ("POST", "/v1/coordination/declare"):
                return _json(200, cell.coordination.declare(json.loads(body)))
            if route == ("POST", "/v1/coordination/claims"):
                return _json(201, cell.coordination.claim(ClaimRequest.model_validate_json(body)))
            if method == "POST" and path.endswith("/release"):
                return _json(200, cell.coordination.release(unquote(path.split("/")[4])))
            return _problem(404)
        if path.startswith("/v1/agent-state"):
            cell.agent_features.need("agent_state")
            if route == ("PUT", "/v1/agent-state"):
                write = AgentStateWrite.model_validate_json(body)
                return _json(
                    200, cell.state.write_agent_state(write.model_dump(mode="json", exclude_none=True))
                )
            if route == ("POST", "/v1/agent-state/read"):
                return _json(200, cell.state.read_agent_state(json.loads(body)))
            return _problem(404)
        if method == "POST" and path.startswith("/v1/state/refresh-requests/") and path.endswith("/release"):
            cell.agent_features.need("state")
            request_id = unquote(path.split("/")[-2])
            return _json(200, cell.state.release(request_id, json.loads(body)))
        if route in (
            ("POST", "/v1/state/verify"),
            ("GET", "/v1/state/refresh-requests"),
            ("POST", "/v1/objects/push"),
        ):
            cell.agent_features.need("state")
            if route == ("POST", "/v1/state/verify"):
                return _json(200, cell.state.verify(json.loads(body)))
            if route == ("POST", "/v1/objects/push"):
                return _json(200, cell.state.push(json.loads(body)))
            return _json(200, cell.state.lease_refreshes(int(query.get("limit", ["50"])[0])))
        if path.startswith("/v1/measure/counterfactual-runs"):
            cell.agent_features.need("measurement")
            if route == ("POST", "/v1/measure/counterfactual-runs"):
                return _json(201, cell.measure.counterfactual(json.loads(body)))
            if route == ("GET", "/v1/measure/counterfactual-runs"):
                return _json(200, cell.measure.listed())
            found = cell.measure.read(unquote(path.rsplit("/", 1)[1])) if method == "GET" else None
            return _json(200, found) if found is not None else _problem(404)
        if path.startswith(("/v1/scenarios", "/v1/replay/", "/v1/scenario-runs")):
            cell.agent_features.need("turns")
            return self._replay(method, path, query, body)
        return None

    def _check(self, request: CheckRequest) -> dict[str, Any]:
        suppressed = request.subject is not None and self.cell.agent_features.suppressed(
            request.subject, request.purpose, request.channel
        )
        return self.cell.coordination.check(request, suppressed)

    def _replay(self, method: str, path: str, query: dict[str, list[str]], body: bytes) -> Response:
        replay = self.cell.replay
        if (method, path) == ("POST", "/v1/scenarios"):
            return _json(201, replay.create(json.loads(body)))
        if (method, path) == ("GET", "/v1/scenarios"):
            ids = [i for raw in query.get("ids", []) for i in raw.split(",") if i]
            return _json(200, replay.listed(ids or None))
        if method == "GET" and path.startswith("/v1/scenarios/"):
            return _json(200, replay.get(unquote(path.rsplit("/", 1)[1])))
        if (method, path) == ("POST", "/v1/replay/cases"):
            return _json(200, replay.case(json.loads(body)))
        if (method, path) == ("POST", "/v1/scenario-runs"):
            return _json(201, replay.run(json.loads(body)))
        if method == "GET" and path.startswith("/v1/scenario-runs/"):
            return _json(200, replay.read_run(unquote(path.rsplit("/", 1)[1])))
        return _problem(404)

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
            ("POST", "/v1/context/prefetch"): self._prefetch,
            ("POST", "/v1/history/search"): self._search,
            ("POST", "/v1/history/timeline"): self._timeline,
            ("POST", "/v1/history/open"): self._open,
            ("POST", "/v1/subject-tokens"): self._subject_token,
            ("POST", "/v1/feedback"): self._feedback,
        }
        if (method, path) in routes:
            return routes[(method, path)](body)
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

    def _writer(self, headers: Headers) -> bool:
        return headers.get("authorization", "").partition(" ")[2] in self.cell.agent_memory_writers

    def _tools(self, query: dict[str, list[str]], headers: Headers) -> Response:
        with_memory = query.get("agent_memory", ["false"])[0].lower() == "true"
        return _json(200, definitions(agent_memory=with_memory, write_agent_memory=self._writer(headers)))

    def _agent_memory(
        self, method: str, rest: str, query: dict[str, list[str]], headers: Headers, body: bytes
    ) -> Response:
        """`/v1/agent-memory/*`, as the cell answers it. Writes need the `agent_memory:write` scope."""
        if method in ("POST", "PATCH", "DELETE") and rest != "search" and not self._writer(headers):
            return _problem(403, "the key lacks the scope agent_memory:write", code="scope_missing")
        parts = [unquote(p) for p in rest.split("/")] if rest else []
        route = (method, *parts[:1], *(["{id}"] if len(parts) >= 2 else []), *parts[2:3])
        one = parts[1] if len(parts) >= 2 else ""
        try:
            with self.cell._lock:
                return self._agent_memory_route(route, one, query, headers, body)
        except PersonalDataError:
            detail = "the note has personal data; write it so it helps with any customer"
            return _problem(422, detail, code="personal_data_in_agent_memory")
        except KeyError:
            return _problem(404)

    def _agent_memory_route(
        self, route: tuple[str, ...], one: str, query: dict[str, list[str]], headers: Headers, body: bytes
    ) -> Response:
        store = self.cell.agent_memory

        def arg(name: str, default: str | None = None) -> str | None:
            return query.get(name, [default])[0] if query.get(name) or default is not None else None

        limit = int(arg("limit", "20") or 20)
        if route == ("GET", "block"):
            max_tokens = int(arg("max_tokens", "300") or 300)
            if not 50 <= max_tokens <= 2000:
                return _problem(422, "max_tokens: between 50 and 2000")
            block = store.block(max_tokens, query.get("tags", []), arg("view"))
            if headers.get("if-none-match") == block.etag:
                return Response(304, b"", {"etag": block.etag})
            return _json(200, block.model_dump(mode="json"), {"etag": block.etag})
        if route == ("POST", "search"):
            search = AgentMemorySearchRequest.model_validate_json(body)
            notes = store.search(search.query, list(search.tags), search.limit)
            return _json(200, {"notes": [n.model_dump(mode="json") for n in notes]})
        if route == ("GET", "tools"):
            return _json(200, definitions(agent_memory=True, write_agent_memory=self._writer(headers))[3:])
        if route == ("POST", "notes"):
            result = store.create(CreateAgentNoteRequest.model_validate_json(body))
            return Response(201, result.model_dump_json(exclude={"error"}).encode(), _JSON)
        if route == ("GET", "notes"):
            return _model(store.page(arg("status"), arg("visibility"), arg("cursor"), limit))
        if route == ("DELETE", "notes"):
            return _model(store.erase(arg("source_id") or MOCK_SOURCE))
        if route == ("GET", "notes", "{id}"):
            return _model(store.get(one))
        if route == ("PATCH", "notes", "{id}"):
            return _model(store.update(one, UpdateAgentNoteRequest.model_validate_json(body)))
        if route == ("POST", "notes", "{id}", "retire"):
            return _model(store.retire(one))
        if route == ("GET", "notes", "{id}", "versions"):
            return _json(200, [n.model_dump(mode="json") for n in store.versions(one)])
        if route == ("GET", "export"):
            return _model(store.export(arg("source_id") or MOCK_SOURCE))
        if route == ("POST", "distill"):
            request = DistillRequest.model_validate_json(body)
            session = request.conversation_id or request.task_id
            actions = [
                f"{e.item.action.operation} {' '.join(r.type for r in e.item.object_refs)}".strip()
                for e in self.cell.events
                if e.item.action is not None and session in (e.item.conversation_id, e.item.task_id)
            ]
            return Response(201, store.distill(request, actions).model_dump_json().encode(), _JSON)
        if route == ("GET", "proposals"):
            return _model(store.proposal_page(arg("status"), arg("cursor"), limit))
        if route in (("POST", "proposals", "{id}", "approve"), ("POST", "proposals", "{id}", "reject")):
            try:
                return _model(store.decide(one, approve=route[-1] == "approve"))
            except ValueError:
                return _problem(409, "the proposal was already decided")
        return _problem(404)

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

    def _turns(self, method: str, path: str, headers: Headers, body: bytes) -> Response:
        if "turns" not in self.cell.features:
            return _problem(404)
        if method == "POST" and path == "/v1/turns":
            if headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            data = json.loads(body or b"null")
            items = data.get("turns") if isinstance(data, dict) else None
            if not isinstance(items, list) or not 1 <= len(items) <= MAX_TURNS:
                return _problem(422, f"turns: between 1 and {MAX_TURNS} turn records")
            result = self.cell.turns.record(items)
            return _json(207 if result.errors else 200, result.model_dump(mode="json"))
        if method == "POST" and path == "/v1/turns/promote":
            return _model(self.cell.turns.promote(PromoteRequest.model_validate_json(body)))
        if method == "GET" and path.count("/") == 3:
            view = self.cell.turns.read(unquote(path.rsplit("/", 1)[1]))
            return (
                _problem(404)
                if view is None
                else _json(200, view.model_dump(mode="json", by_alias=True, exclude_none=True))
            )
        return _problem(405)

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
        request = ContextRequest.model_validate_json(body)
        asked: list[str] = list(request.include or ())
        blocks = self.cell.agent_features.blocks_for(asked, request.subject)
        response = self.cell.context(request)
        if response.path != "holdout":
            failed = bool(set(asked) & self.cell.agent_features.failing)
            response = response.model_copy(update={**blocks, "degraded": response.degraded or failed})
        return _model(response)

    def _prefetch(self, body: bytes) -> Response:
        self.cell.prefetch(PrefetchRequest.model_validate_json(body))
        return _json(202, {})

    def _search(self, body: bytes) -> Response:
        return _model(self.cell.search(SearchRequest.model_validate_json(body)))

    def _timeline(self, body: bytes) -> Response:
        return _model(self.cell.timeline(TimelineRequest.model_validate_json(body)))

    def _open(self, body: bytes) -> Response:
        request = OpenItemRequest.model_validate_json(body)
        return _model(self.cell.open(request.item_id, request.verification, request.subject))

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
