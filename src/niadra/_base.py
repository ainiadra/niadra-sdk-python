"""What the sync and async clients share: configuration, argument coercion, request building
and the fail-open policy. Nothing in here does I/O.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, TypeVar
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ValidationError

from niadra._cache import ContextCache
from niadra._ids import new_key
from niadra._queue import EventBuffer, serialize
from niadra._transport import Request
from niadra._turns import MIN_PREFETCH, NO_PREFETCH, TurnSupport, turn_text
from niadra.errors import APIError, ConfigurationError
from niadra.keys import ApiKey
from niadra.models.agent_memory import AgentMemory, AgentMemorySearchRequest, CreateAgentNoteRequest, Evidence
from niadra.models.common import Handle, ObjectRef
from niadra.models.context import (
    ContextRequest,
    HistoryFilters,
    OpenItemRequest,
    PrefetchRequest,
    SearchRequest,
    TargetModel,
    TimelineRequest,
)
from niadra.models.events import (
    ActionInfo,
    Closes,
    ContextStamp,
    ConversationEndedItem,
    EventItem,
    FeedbackAction,
    FeedbackRequest,
    HandoffItem,
    IdentifyItem,
    MediaUploadRequest,
    MediaUploadResponse,
    SpeakerRef,
    TaskEndedItem,
    VerifyItem,
    VerifyMethod,
)
from niadra.models.results import Context
from niadra.models.tokens import SubjectTokenRequest
from niadra.options import CacheOptions, QueueOptions, Timeouts
from niadra.vocabulary import AssertionMethod, EventKind, Speaker, SubjectKind, Verification

logger = logging.getLogger("niadra")

T = TypeVar("T")

HandleLike = Handle | Mapping[str, Any]
ObjectLike = ObjectRef | str | Mapping[str, Any]
TargetLike = TargetModel | str | Mapping[str, Any]
ClosesLike = Closes | str | Mapping[str, Any]
StampLike = ContextStamp | Mapping[str, Any]
VerificationLike = Verification | str
ItemLike = BaseModel | Mapping[str, Any]
HandoffTarget = Literal["human", "agent"]
HandoffMode = Literal["warm", "cold"]

_ITEM_TYPES: dict[str, type[BaseModel]] = {
    "event": EventItem,
    "identify": IdentifyItem,
    "verify": VerifyItem,
    "conversation.ended": ConversationEndedItem,
    "task.ended": TaskEndedItem,
    "handoff": HandoffItem,
}
_BATCH_ITEM_TYPES = (EventItem, IdentifyItem, VerifyItem, ConversationEndedItem, TaskEndedItem, HandoffItem)

_warned_no_key = False
_warn_lock = threading.Lock()


def _warn_disabled(reason: str) -> None:
    global _warned_no_key
    with _warn_lock:
        if _warned_no_key:
            return
        _warned_no_key = True
    logger.warning("niadra: %s; the client is disabled and every call is a no-op", reason)


def describe(error: BaseException) -> str:
    """A log-safe description of an error: class, status and catalog code, never field values.

    Validation messages from Pydantic echo the input, which may be a phone number or a
    message, so only field paths and error types are kept.
    """
    if isinstance(error, ValidationError):
        fields = [_location(e.get("loc", ())) + ":" + str(e.get("type")) for e in error.errors()]
        return "invalid input: " + ", ".join(fields)[:300]
    if isinstance(error, APIError):
        suffix = f", request_id={error.request_id}" if error.request_id else ""
        return f"HTTP {error.status_code} {error.code}{suffix}"
    if isinstance(error, (ValueError, TypeError)):
        return type(error).__name__
    return f"{type(error).__name__}: {error}"


def _location(parts: Any) -> str:
    return ".".join(str(part) for part in parts)


def error_code(error: BaseException) -> str:
    """The catalog code of an API error, or the exception's class name."""
    code = getattr(error, "code", None)
    return str(code) if code else type(error).__name__


def as_handle(value: HandleLike) -> Handle:
    return value if isinstance(value, Handle) else Handle.model_validate(value)


def as_object(value: ObjectLike) -> ObjectRef:
    if isinstance(value, ObjectRef):
        return value
    if isinstance(value, str):
        return ObjectRef.parse(value)
    return ObjectRef.model_validate(value)


def as_target(value: TargetLike) -> TargetModel:
    if isinstance(value, TargetModel):
        return value
    if isinstance(value, str):
        return TargetModel.parse(value)
    return TargetModel.model_validate(value)


def as_closes(value: ClosesLike) -> Closes:
    if isinstance(value, Closes):
        return value
    if isinstance(value, str):
        return Closes(item_id=value)
    return Closes.model_validate(value)


def as_stamp(value: StampLike) -> ContextStamp:
    return value if isinstance(value, ContextStamp) else ContextStamp.model_validate(value)


def object_path(value: ObjectLike) -> str:
    """`/v1/objects/{type}/{namespace}/{id}` for an object reference.

    The segments are system record ids, never personal data, so they may go in the path. A
    slash cannot: the server's route would split on it, even percent-encoded.
    """
    ref = as_object(value)
    segments = (ref.type, ref.namespace, ref.id)
    if any("/" in segment for segment in segments):
        raise ValueError("object type, namespace and id cannot contain a slash")
    return "/v1/objects/" + "/".join(quote(segment, safe="") for segment in segments)


def as_item(value: ItemLike) -> BaseModel:
    """Accepts a batch item model, or a mapping that becomes one (an event unless `type` says otherwise)."""
    if isinstance(value, _BATCH_ITEM_TYPES):
        return value
    if isinstance(value, BaseModel):
        raise TypeError(f"{type(value).__name__} is not a batch item")
    item_type = str(value.get("type", "event"))
    model = _ITEM_TYPES.get(item_type)
    if model is None:
        raise ValueError(f"unknown item type {item_type!r}")
    return model.model_validate(value)


class ClientCore:
    """State and pure helpers common to `Niadra` and `AsyncNiadra`."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        *,
        channel: str | None,
        strict: bool,
        timeouts: Timeouts | None,
        cache: CacheOptions | None,
        queue: QueueOptions | None,
    ) -> None:
        self.strict = strict
        self.channel = channel
        self.timeouts = timeouts or Timeouts()
        self.cache_options = cache or CacheOptions()
        self.queue_options = queue or QueueOptions()
        self.buffer = EventBuffer(self.queue_options)
        self.key: ApiKey | None = None
        self.api_key = ""
        self.base_url = ""
        self.enabled = False
        self.agent_memory_cache = AgentMemoryCache(self.cache_options)
        self.turns = TurnSupport()

        raw = api_key if api_key is not None else os.environ.get("NIADRA_API_KEY", "")
        base_url = base_url or os.environ.get("NIADRA_BASE_URL") or None
        if not raw.strip():
            if strict:
                raise ConfigurationError("no Niadra key: pass api_key= or set NIADRA_API_KEY")
            _warn_disabled("no API key (pass api_key= or set NIADRA_API_KEY)")
            return
        try:
            self.key = ApiKey.parse(raw)
        except ConfigurationError:
            if base_url is None:
                if strict:
                    raise
                _warn_disabled("the API key is malformed")
                return
        self.api_key = raw.strip()
        self.base_url = (base_url or (self.key.base_url if self.key else "")).rstrip("/")
        self.enabled = True

    def fail(self, method: str, error: BaseException, default: T) -> T:
        if self.strict:
            raise error
        logger.warning("niadra: %s failed, continuing without it (%s)", method, describe(error))
        return default

    def context_budget(self, view: str, timeout: float | None) -> float:
        if timeout is not None:
            return timeout
        return self.timeouts.context_voice if view == "voice" else self.timeouts.context

    def navigation_budget(self, voice: bool, timeout: float | None) -> float:
        if timeout is not None:
            return timeout
        return self.timeouts.navigation_voice if voice else self.timeouts.navigation

    def context_request(
        self,
        subject: HandleLike | None,
        object: ObjectLike | None,
        about: HandleLike | None,
        view: str,
        verification: VerificationLike,
        conversation_id: str | None,
        task_id: str | None,
        query: str | None,
        delta: bool,
        target: TargetLike | None,
        format: Literal["text", "json"] = "text",
    ) -> ContextRequest:
        return ContextRequest(
            subject=as_handle(subject) if subject is not None else None,
            object=as_object(object) if object is not None else None,
            about=as_handle(about) if about is not None else None,
            view=view,
            verification=Verification(verification),
            conversation_id=conversation_id,
            task_id=task_id,
            query=query,
            delta=delta,
            target=as_target(target) if target is not None else None,
            format="json" if format == "json" else None,
        )

    def turn_query(self, request: ContextRequest, turn: str | None) -> str | None:
        """The customer's turn to send as `query`, or None: a read with its own `query`, a blank turn,
        or a space that answered without slots a moment ago keep the read as it was."""
        if request.query is not None or not self.turns.wanted():
            return None
        return turn_text(turn)

    @staticmethod
    def unpinned(fetched: Context) -> Context:
        """An answer compiled for the turn by a space without memory v2: served once, never kept."""
        unpinned = fetched.model_copy()
        unpinned._unpinned = True
        return unpinned

    def settle_turn(self, cache: ContextCache, key: str | None, scope: str, fetched: Context) -> Context:
        """What a read that sent the turn returns when the space read it (or the answer cannot tell):
        the pack is the conversation's pinned one, cached as the read without `query`, and the slots
        are this turn's, on the answer only."""
        if key is None:
            return fetched
        pack = fetched.pack
        stored = cache.absorb(
            key,
            scope,
            fetched.model_copy(
                update={"slots": None, "pack": pack.model_copy(update={"slots": []}) if pack else None}
            ),
        )
        kept = stored.pack
        if kept is not None and pack is not None:
            kept = kept.model_copy(update={"slots": pack.slots})
        return stored.model_copy(update={"slots": fetched.slots, "pack": kept})

    def prefetch_request(
        self,
        subject: HandleLike | None,
        object: ObjectLike | None,
        about: HandleLike | None,
        view: str,
        verification: VerificationLike,
        conversation_id: str | None,
        task_id: str | None,
        text: str | None,
    ) -> PrefetchRequest | None:
        """The body of a prefetch, or None when there is nothing worth sending."""
        query = turn_text(text)
        if query is None or len(query) < MIN_PREFETCH or not self.turns.prefetch_wanted():
            return None
        return PrefetchRequest(
            subject=as_handle(subject) if subject is not None else None,
            object=as_object(object) if object is not None else None,
            about=as_handle(about) if about is not None else None,
            view=view,
            verification=Verification(verification),
            conversation_id=conversation_id,
            task_id=task_id,
            query=query,
        )

    def prefetch_http(self, request: PrefetchRequest) -> Request:
        budget = self.timeouts.prefetch
        body = request.model_dump(mode="json", exclude_none=True)
        return Request(
            "POST", "/v1/context/prefetch", json=body, timeout=budget, budget=budget, max_attempts=1
        )

    def prefetch_failed(self, error: BaseException) -> None:
        """A prefetch never fails anything; a server without the route is not asked again for a while."""
        if isinstance(error, APIError) and error.status_code in NO_PREFETCH:
            self.turns.prefetch_refused()
        logger.debug("niadra: prefetch skipped (%s)", type(error).__name__)

    def cacheable(self, request: ContextRequest, use_cache: bool) -> bool:
        return use_cache and self.cache_options.enabled and bool(request.conversation_id or request.task_id)

    @staticmethod
    def scope_of(conversation_id: str | None, task_id: str | None) -> str:
        return f"c:{conversation_id}" if conversation_id else f"t:{task_id}" if task_id else ""

    @staticmethod
    def context_http(request: ContextRequest, budget: float, known_etag: str | None) -> Request:
        body = request.model_copy(update={"known_etag": known_etag}).model_dump(
            mode="json", exclude_none=True
        )
        return Request("POST", "/v1/context", json=body, timeout=budget, budget=budget)

    @staticmethod
    def parse_context(data: Any, started: float) -> Context:
        context = Context.model_validate(data)
        return context.model_copy(update={"elapsed_ms": round((time.monotonic() - started) * 1000, 1)})

    def search_http(
        self,
        subject: HandleLike,
        query: str,
        about: HandleLike | None,
        filters: HistoryFilters | Mapping[str, Any] | None,
        max_tokens: int,
        verification: VerificationLike,
        conversation_id: str | None,
        task_id: str | None,
        budget: float,
        limit: int | None = None,
    ) -> Request:
        body = SearchRequest(
            subject=as_handle(subject),
            about=as_handle(about) if about is not None else None,
            query=query,
            filters=_filters(filters),
            max_tokens=max_tokens,
            verification=Verification(verification),
            conversation_id=conversation_id,
            task_id=task_id,
            limit=limit,
        )
        return Request(
            "POST",
            "/v1/history/search",
            json=_body(body),
            timeout=budget,
            budget=budget,
        )

    def timeline_http(
        self,
        subject: HandleLike,
        about: HandleLike | None,
        filters: HistoryFilters | Mapping[str, Any] | None,
        cursor: str | None,
        limit: int,
        verification: VerificationLike,
        conversation_id: str | None,
        budget: float,
    ) -> Request:
        # POST rather than GET: the subject handle is personal data and never goes in a URL.
        body = TimelineRequest(
            subject=as_handle(subject),
            about=as_handle(about) if about is not None else None,
            filters=_filters(filters),
            cursor=cursor,
            limit=limit,
            verification=Verification(verification),
            conversation_id=conversation_id,
        )
        return Request(
            "POST",
            "/v1/history/timeline",
            json=_body(body),
            timeout=budget,
            budget=budget,
        )

    @staticmethod
    def open_http(
        item_id: str,
        verification: VerificationLike,
        conversation_id: str | None,
        subject: HandleLike | None,
        budget: float,
    ) -> Request:
        # POST rather than GET: a conversation id may be a phone number or an e-mail, and the customer
        # is personal data; both go in the body, never in a URL.
        if not item_id:
            raise ValueError("item_id must not be empty")
        body = OpenItemRequest(
            item_id=item_id,
            subject=as_handle(subject) if subject is not None else None,
            verification=Verification(verification),
            conversation_id=conversation_id,
        )
        return Request("POST", "/v1/history/open", json=_body(body), timeout=budget, budget=budget)

    def batch_http(self, payloads: list[dict[str, Any]], budget: float | None = None) -> Request:
        """A batch. The background queue sends it without a budget; a caller waiting on it, with one."""
        return Request(
            "POST",
            "/v1/batch",
            json={"items": payloads},
            timeout=self.timeouts.write,
            budget=budget,
            idempotency_key=new_key(),
        )

    def subject_token_http(
        self,
        subject: HandleLike,
        about: HandleLike | None,
        conversation_id: str | None,
        task_id: str | None,
        verification: VerificationLike,
    ) -> Request:
        body = SubjectTokenRequest(
            subject=as_handle(subject),
            about=as_handle(about) if about is not None else None,
            conversation_id=conversation_id,
            task_id=task_id,
            verification=Verification(verification),
        )
        return Request(
            "POST",
            "/v1/subject-tokens",
            json=_body(body),
            timeout=self.timeouts.write,
            budget=self.timeouts.write,
            idempotency_key=new_key(),
        )

    def enqueue(self, item: ItemLike) -> bool:
        model = as_item(item)
        payload = serialize(model)
        return payload is not None and self.buffer.put(payload)

    def default_channel(self, channel: str | None) -> str:
        chosen = channel or self.channel
        if not chosen:
            raise ValueError("no channel: pass channel= here or when creating the client")
        return chosen

    def action_item(
        self,
        operation: str,
        *,
        subject: HandleLike | None,
        object: ObjectLike | None,
        result: str | None,
        purpose: str | None,
        closes: ClosesLike | None,
        conversation_id: str | None,
        task_id: str | None,
        channel: str | None,
        speaker: Speaker | str,
        speaker_id: str | None,
        corrects_action_id: str | None,
        occurred_at: datetime | None,
        idempotency_key: str | None,
        context_stamp: StampLike | None = None,
    ) -> EventItem:
        fields: dict[str, Any] = {}
        if context_stamp is not None:
            fields["context_stamp"] = as_stamp(context_stamp)
        if occurred_at is not None:
            fields["occurred_at"] = occurred_at
        if idempotency_key is not None:
            fields["idempotency_key"] = idempotency_key
        return EventItem(
            kind=EventKind.ACTION,
            channel=self.default_channel(channel),
            conversation_id=conversation_id,
            task_id=task_id,
            handles=[as_handle(subject)] if subject is not None else [],
            object_refs=[as_object(object)] if object is not None else [],
            speaker=SpeakerRef(role=Speaker(speaker), id=speaker_id),
            action=ActionInfo(
                operation=operation,
                result=result,
                purpose=purpose,
                closes=as_closes(closes) if closes is not None else None,
                corrects_action_id=corrects_action_id,
            ),
            **fields,
        )

    @staticmethod
    def identify_item(
        handles: Sequence[HandleLike],
        method: AssertionMethod | str,
        subject_kind: SubjectKind | str,
        conversation_id: str | None,
    ) -> IdentifyItem:
        return IdentifyItem(
            handles=[as_handle(h) for h in handles],
            method=AssertionMethod(method),
            subject_kind=SubjectKind(subject_kind),
            conversation_id=conversation_id,
        )

    @staticmethod
    def verify_item(
        method: VerifyMethod,
        level: VerificationLike,
        handle: HandleLike,
        conversation_id: str | None,
        task_id: str | None,
        valid_until: datetime | None,
    ) -> VerifyItem:
        return VerifyItem(
            method=method,
            level=Verification(level),
            handle=as_handle(handle),
            conversation_id=conversation_id,
            task_id=task_id,
            valid_until=valid_until,
        )

    def object_http(self, object: ObjectLike, voice: bool, timeout: float | None) -> Request:
        budget = self.navigation_budget(voice, timeout)
        return Request("GET", object_path(object), timeout=budget, budget=budget)

    def object_timeline_http(
        self, object: ObjectLike, cursor: str | None, limit: int, voice: bool, timeout: float | None
    ) -> Request:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        params = {"limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        budget = self.navigation_budget(voice, timeout)
        return Request("GET", object_path(object) + "/timeline", params=params, timeout=budget, budget=budget)

    def feedback_http(self, request: FeedbackRequest) -> Request:
        return Request(
            "POST",
            "/v1/feedback",
            json=_body(request),
            timeout=self.timeouts.write,
            budget=self.timeouts.write,
            idempotency_key=request.idempotency_key,
        )

    def feedback_batch_http(self, requests: Sequence[FeedbackRequest]) -> Request:
        body = {"items": [_body(r) for r in requests]}
        return Request(
            "POST", "/v1/feedback/batch", json=body, timeout=self.timeouts.write, budget=self.timeouts.write
        )

    def ingest_status_http(self, conversation_id: str | None, task_id: str | None) -> Request:
        if (conversation_id is None) == (task_id is None):
            raise ValueError("pass exactly one of conversation_id or task_id")
        body = {"conversation_id": conversation_id} if conversation_id else {"task_id": task_id}
        budget = self.timeouts.write
        return Request("POST", "/v1/ingest/status", json=body, timeout=budget, budget=budget)

    def whoami_http(self) -> Request:
        return Request("GET", "/v1/sources/me", timeout=self.timeouts.write, budget=self.timeouts.write)

    @staticmethod
    def feedback_item(item: FeedbackRequest | Mapping[str, Any]) -> FeedbackRequest:
        if isinstance(item, FeedbackRequest):
            return item
        fields = dict(item)
        if "subject" in fields:
            fields["subject"] = as_handle(fields["subject"])
        return FeedbackRequest.model_validate(fields)

    @staticmethod
    def feedback_request(
        action: FeedbackAction,
        subject: HandleLike,
        fact_id: str | None,
        open_item_id: str | None,
        conversation_id: str | None,
        value: str | None,
        reason: str | None,
        idempotency_key: str | None,
    ) -> FeedbackRequest:
        fields: dict[str, Any] = {}
        if idempotency_key is not None:
            fields["idempotency_key"] = idempotency_key
        return FeedbackRequest(
            subject=as_handle(subject),
            action=action,
            fact_id=fact_id,
            open_item_id=open_item_id,
            conversation_id=conversation_id,
            value=value,
            reason=reason,
            **fields,
        )

    def media_upload_http(
        self, data: bytes, content_type: str, subject: HandleLike | None
    ) -> tuple[Request, str]:
        """The request that reserves an upload, and the digest the event will carry."""
        digest = hashlib.sha256(data).hexdigest()
        body = MediaUploadRequest(
            content_type=content_type,
            size_bytes=len(data),
            sha256=digest,
            subject=as_handle(subject) if subject is not None else None,
        )
        request = Request(
            "POST",
            "/v1/media/uploads",
            json=_body(body),
            timeout=self.timeouts.write,
            budget=self.timeouts.write,
            idempotency_key=new_key(),
        )
        return request, digest

    @staticmethod
    def upload_headers(reserved: MediaUploadResponse, content_type: str) -> dict[str, str]:
        """Exactly what the signature covers; a server that names nothing still signs the type."""
        return dict(reserved.upload_headers) or {"Content-Type": content_type}

    def check_upload_url(self, url: str) -> None:
        """The bytes go straight to storage, so the URL must be HTTPS unless the API itself is not.

        Only the local emulator is served over plain HTTP; anywhere else a downgrade would send
        the customer's media in the clear.
        """
        scheme = urlsplit(url).scheme
        if scheme == "https" or (scheme == "http" and urlsplit(self.base_url).scheme == "http"):
            return
        raise ValueError("the upload URL is not HTTPS")

    def agent_memory_http(
        self, max_tokens: int, tags: Sequence[str] | None, view: str | None, etag: str | None, budget: float
    ) -> Request:
        params: dict[str, Any] = {"max_tokens": str(max_tokens)}
        if tags:
            params["tags"] = list(tags)
        if view:
            params["view"] = view
        headers = {"If-None-Match": etag} if etag else None
        return Request(
            "GET", "/v1/agent-memory/block", params=params, headers=headers, timeout=budget, budget=budget
        )

    def search_agent_memory_http(
        self,
        query: str,
        tags: Sequence[str] | None,
        limit: int,
        conversation_id: str | None,
        task_id: str | None,
        budget: float,
    ) -> Request:
        body = AgentMemorySearchRequest(
            query=query, tags=list(tags or []), limit=limit, conversation_id=conversation_id, task_id=task_id
        )
        return Request("POST", "/v1/agent-memory/search", json=_body(body), timeout=budget, budget=budget)

    def remember_http(
        self,
        kind: str,
        title: str,
        body: str,
        tags: Sequence[str] | None,
        evidence: Evidence | Mapping[str, Any] | None,
        visibility: str | None,
        valid_until: datetime | None,
    ) -> Request:
        request = CreateAgentNoteRequest.model_validate(
            {
                "kind": kind,
                "title": title,
                "body": body,
                "tags": list(tags or []),
                "evidence": evidence,
                "valid_until": valid_until,
                **({"visibility": visibility} if visibility else {}),
            }
        )
        budget = self.timeouts.write
        # A retried write must not save the note twice.
        return Request(
            "POST",
            "/v1/agent-memory/notes",
            json=_body(request),
            timeout=budget,
            budget=budget,
            idempotency_key=new_key(),
        )

    def agent_memory_failed(self, key: str, error: BaseException) -> AgentMemory:
        """A block that could not be read: `enabled=False` when the route is not there (501), else the
        last good block within `max_stale`, else an empty one with the error's code."""
        if isinstance(error, APIError) and error.status_code == 501:
            disabled = AgentMemory.empty(enabled=False, error="not_implemented")
            self.agent_memory_cache.put(key, disabled)
            return disabled
        fallback = self.agent_memory_cache.fallback(key)
        if fallback is not None and not self.strict:
            logger.warning("niadra: agent memory failed, serving the last good block (%s)", error_code(error))
            return fallback
        return self.fail("agent_memory", error, AgentMemory.empty(error=error_code(error)))

    @staticmethod
    def handoff_item(
        conversation_id: str,
        target: HandoffTarget,
        target_source: str | None,
        reason: str | None,
        mode: HandoffMode,
    ) -> HandoffItem:
        return HandoffItem(
            conversation_id=conversation_id,
            target=target,
            target_source=target_source,
            reason=reason,
            mode=mode,
        )


class AgentMemoryCache:
    """The agent memory blocks already read, per (max_tokens, tags, view), with their ETags.

    A block younger than the context cache's `ttl` is served as is; an older one is revalidated
    with `If-None-Match`. On failure, the last good block is served for up to `max_stale`.
    """

    def __init__(self, options: CacheOptions) -> None:
        self._options = options
        self._entries: dict[str, tuple[AgentMemory, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(max_tokens: int, tags: Sequence[str] | None, view: str | None) -> str:
        return f"{max_tokens}|{','.join(sorted(tags or []))}|{view or ''}"

    def get(self, key: str) -> tuple[AgentMemory | None, bool]:
        """The cached block and whether it is still fresh."""
        if not self._options.enabled:
            return None, False
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None, False
        block, stored_at = entry
        return block, time.monotonic() - stored_at < self._options.ttl

    def put(self, key: str, block: AgentMemory) -> AgentMemory:
        if self._options.enabled:
            with self._lock:
                self._entries[key] = (block, time.monotonic())
        return block

    def absorb(self, key: str, data: Any) -> AgentMemory:
        """A new block from the API, or the cached one when the API answered 304."""
        cached, _ = self.get(key)
        if data is None and cached is not None:
            return self.put(key, cached.model_copy(update={"source": "cache"}))
        block = AgentMemory.model_validate({**(data or {}), "source": "network"})
        if not block.enabled:
            # The agent memory is off in the space: nothing goes into the prompt.
            block = block.model_copy(update={"text": "", "notes": []})
        return self.put(key, block)

    def fallback(self, key: str) -> AgentMemory | None:
        with self._lock:
            entry = self._entries.get(key)
        if entry is None or not entry[0].text:
            return None
        block, stored_at = entry
        if time.monotonic() - stored_at > self._options.max_stale:
            return None
        return block.model_copy(update={"source": "cache"})


def _body(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _filters(value: HistoryFilters | Mapping[str, Any] | None) -> HistoryFilters:
    if value is None:
        return HistoryFilters()
    return value if isinstance(value, HistoryFilters) else HistoryFilters.model_validate(value)
