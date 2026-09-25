from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from niadra._admin import AsyncAdmin
from niadra._base import (
    ClientCore,
    ClosesLike,
    HandleLike,
    HandoffMode,
    HandoffTarget,
    ItemLike,
    ObjectLike,
    StampLike,
    TargetLike,
    VerificationLike,
    error_code,
    logger,
)
from niadra._cache import ContextCache, cache_key
from niadra._queue import AsyncFlusher, is_retryable
from niadra._transport import AsyncTransport
from niadra.conversation import AsyncConversation, AsyncTask
from niadra.models.admin import KeyIdentity
from niadra.models.agent_memory import (
    AgentMemory,
    AgentMemorySearchResponse,
    AgentNote,
    AgentNoteKind,
    AgentNoteVisibility,
    Evidence,
    RememberResult,
)
from niadra.models.context import ContextRequest, HistoryFilters, ObjectState, OpenedItem
from niadra.models.events import (
    BatchResponse,
    FeedbackAction,
    FeedbackRequest,
    MediaUploadResponse,
    VerifyMethod,
)
from niadra.models.objects import ObjectTimeline
from niadra.models.results import Context, MediaUpload, SearchResult, TimelinePage
from niadra.models.tokens import SubjectToken
from niadra.options import CacheOptions, QueueOptions, Timeouts
from niadra.tools import AsyncToolKit, definitions
from niadra.vocabulary import AssertionMethod, Speaker, SubjectKind, Verification


class AsyncNiadra:
    """The asyncio Niadra client. Same methods and behavior as `Niadra`, awaited.

    `track()`, `action()` and `handoff()` stay plain methods: they only queue, and a task on
    the running loop sends batches. Outside a running loop, queued items wait for `flush()`.
    Use `async with AsyncNiadra() as niadra:` or `await niadra.close()` to flush before exit.

    Takes the same arguments as `Niadra`, with an `httpx.AsyncClient` as `http_client`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        channel: str | None = None,
        strict: bool = False,
        timeouts: Timeouts | None = None,
        cache: CacheOptions | None = None,
        queue: QueueOptions | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._core = ClientCore(
            api_key, base_url, channel=channel, strict=strict, timeouts=timeouts, cache=cache, queue=queue
        )
        self._cache = ContextCache(self._core.cache_options)
        self._transport = AsyncTransport(self._core.base_url, self._core.api_key, http_client)
        self.admin = AsyncAdmin(self._core, self._transport)
        """Governance calls for a key with the `admin` scope: memory, fact history, corrections, erasure."""
        self._flusher = AsyncFlusher(self._core.buffer, self._send_batch, self._core.queue_options)
        self._refreshes: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def enabled(self) -> bool:
        """False when the client has no usable key and every call is a no-op."""
        return self._core.enabled

    @property
    def base_url(self) -> str:
        return self._core.base_url

    @property
    def mcp_url(self) -> str:
        """The remote MCP endpoint of this space. Connect with the source key and a subject token."""
        return f"{self._core.base_url}/mcp"

    async def context(
        self,
        subject: HandleLike | None = None,
        object: ObjectLike | None = None,
        *,
        about: HandleLike | None = None,
        view: str = "chat",
        verification: VerificationLike = Verification.V0,
        conversation_id: str | None = None,
        task_id: str | None = None,
        query: str | None = None,
        delta: bool = False,
        target: TargetLike | None = None,
        timeout: float | None = None,
        use_cache: bool = True,
        format: Literal["text", "json"] = "text",
    ) -> Context:
        """The context pack for a subject or a business object. See `Niadra.context`."""
        requested = Verification.V0
        try:
            requested = Verification(verification)
            request = self._core.context_request(
                subject,
                object,
                about,
                view,
                verification,
                conversation_id,
                task_id,
                query,
                delta,
                target,
                format,
            )
        except (TypeError, ValueError) as exc:
            return self._core.fail(
                "context", exc, Context.empty(requested=requested, error="invalid_arguments")
            )
        if not self._core.enabled:
            return Context.empty(requested=requested, error="disabled")
        budget = self._core.context_budget(view, timeout)
        if not self._core.cacheable(request, use_cache):
            try:
                return await self._fetch_context(request, budget, None)
            except Exception as exc:
                return self._core.fail(
                    "context", exc, Context.empty(requested=requested, error=error_code(exc))
                )

        key = cache_key(request)
        scope = self._core.scope_of(conversation_id, task_id)
        hit = self._cache.get(key)
        if hit is not None and hit.freshness == "fresh":
            return hit.context
        if hit is not None and hit.freshness == "stale":
            self._refresh_later(key, scope, request, budget)
            return hit.context
        try:
            fetched = await self._fetch_context(request, budget, self._cache.etag(key))
            return self._cache.absorb(key, scope, fetched)
        except Exception as exc:
            fallback = self._cache.fallback(key, exc)
            if fallback is not None and not self._core.strict:
                logger.warning("niadra: context failed, serving the last good pack (%s)", error_code(exc))
                return fallback
            return self._core.fail("context", exc, Context.empty(requested=requested, error=error_code(exc)))

    async def search(
        self,
        subject: HandleLike,
        query: str,
        *,
        about: HandleLike | None = None,
        filters: HistoryFilters | Mapping[str, Any] | None = None,
        max_tokens: int = 800,
        verification: VerificationLike = Verification.V0,
        conversation_id: str | None = None,
        task_id: str | None = None,
        voice: bool = False,
        timeout: float | None = None,
        limit: int | None = None,
    ) -> SearchResult:
        """Searches the subject's whole history by keyword and meaning. See `Niadra.search`."""
        if not self._core.enabled:
            return SearchResult(error="disabled")
        try:
            budget = self._core.navigation_budget(voice, timeout)
            request = self._core.search_http(
                subject,
                query,
                about,
                filters,
                max_tokens,
                verification,
                conversation_id,
                task_id,
                budget,
                limit,
            )
            return SearchResult.model_validate(await self._transport.request(request))
        except Exception as exc:
            return self._core.fail("search", exc, SearchResult(error=error_code(exc)))

    async def timeline(
        self,
        subject: HandleLike,
        *,
        about: HandleLike | None = None,
        filters: HistoryFilters | Mapping[str, Any] | None = None,
        cursor: str | None = None,
        limit: int = 20,
        verification: VerificationLike = Verification.V0,
        conversation_id: str | None = None,
        voice: bool = False,
        timeout: float | None = None,
    ) -> TimelinePage:
        """One page of the subject's history, most recent first. Pass `next_cursor` to go on."""
        if not self._core.enabled:
            return TimelinePage(error="disabled")
        try:
            budget = self._core.navigation_budget(voice, timeout)
            request = self._core.timeline_http(
                subject, about, filters, cursor, limit, verification, conversation_id, budget
            )
            return TimelinePage.model_validate(await self._transport.request(request))
        except Exception as exc:
            return self._core.fail("timeline", exc, TimelinePage(error=error_code(exc)))

    async def open(
        self,
        item_id: str,
        *,
        verification: VerificationLike = Verification.V0,
        conversation_id: str | None = None,
        task_id: str | None = None,  # noqa: ARG002 - kept for 0.1.4 callers; the server never read it here
        subject: HandleLike | None = None,
        voice: bool = False,
        timeout: float | None = None,
    ) -> OpenedItem | None:
        """Opens an episode or object found by `search()` or `timeline()`. None when unavailable."""
        if not self._core.enabled:
            return None
        try:
            budget = self._core.navigation_budget(voice, timeout)
            request = self._core.open_http(item_id, verification, conversation_id, subject, budget)
            return OpenedItem.model_validate(await self._transport.request(request))
        except Exception as exc:
            return self._core.fail("open", exc, None)

    async def object_state(
        self, object: ObjectLike, *, voice: bool = False, timeout: float | None = None
    ) -> ObjectState | None:
        """The derived state of a business object. See `Niadra.object_state`."""
        if not self._core.enabled:
            return None
        try:
            request = self._core.object_http(object, voice, timeout)
            return ObjectState.model_validate(await self._transport.request(request))
        except Exception as exc:
            return self._core.fail("object_state", exc, None)

    async def object_timeline(
        self,
        object: ObjectLike,
        *,
        cursor: str | None = None,
        limit: int = 20,
        voice: bool = False,
        timeout: float | None = None,
    ) -> ObjectTimeline | None:
        """System events and agent actions about one object, newest first. See `Niadra.object_timeline`."""
        if not self._core.enabled:
            return None
        try:
            request = self._core.object_timeline_http(object, cursor, limit, voice, timeout)
            return ObjectTimeline.model_validate(await self._transport.request(request))
        except Exception as exc:
            return self._core.fail("object_timeline", exc, None)

    def track(self, item: ItemLike) -> bool:
        """Queues a batch item and returns at once. Not a coroutine: it never waits. See `Niadra.track`."""
        if not self._core.enabled:
            return False
        try:
            if isinstance(item, Mapping) and item.get("type", "event") == "event" and "channel" not in item:
                item = {**item, "channel": self._core.default_channel(None)}
            queued = self._core.enqueue(item)
        except Exception as exc:
            return self._core.fail("track", exc, False)
        if queued:
            self._flusher.notify()
        return queued

    def action(
        self,
        operation: str,
        *,
        subject: HandleLike | None = None,
        object: ObjectLike | None = None,
        result: str | None = None,
        purpose: str | None = None,
        closes: ClosesLike | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        channel: str | None = None,
        speaker: Speaker | str = Speaker.AI_AGENT,
        speaker_id: str | None = None,
        corrects_action_id: str | None = None,
        occurred_at: datetime | None = None,
        idempotency_key: str | None = None,
        context_stamp: StampLike | None = None,
    ) -> bool:
        """Records what an agent did in a system of record. Queued. See `Niadra.action`."""
        if not self._core.enabled:
            return False
        try:
            item = self._core.action_item(
                operation,
                subject=subject,
                object=object,
                result=result,
                purpose=purpose,
                closes=closes,
                conversation_id=conversation_id,
                task_id=task_id,
                channel=channel,
                speaker=speaker,
                speaker_id=speaker_id,
                corrects_action_id=corrects_action_id,
                occurred_at=occurred_at,
                idempotency_key=idempotency_key,
                context_stamp=context_stamp,
            )
        except Exception as exc:
            return self._core.fail("action", exc, False)
        return self.track(item)

    async def identify(
        self,
        handles: Sequence[HandleLike],
        *,
        method: AssertionMethod | str = AssertionMethod.EXPLICIT_IDENTIFY,
        subject_kind: SubjectKind | str = SubjectKind.PERSON,
        conversation_id: str | None = None,
    ) -> BatchResponse | None:
        """States that two or more handles belong to the same subject. Sent right away."""
        if not self._core.enabled:
            return None
        try:
            item = self._core.identify_item(handles, method, subject_kind, conversation_id)
        except Exception as exc:
            return self._core.fail("identify", exc, None)
        return await self._send_now("identify", item, conversation_id, None)

    async def verify(
        self,
        method: VerifyMethod,
        level: VerificationLike,
        *,
        handle: HandleLike,
        conversation_id: str | None = None,
        task_id: str | None = None,
        valid_until: datetime | None = None,
    ) -> BatchResponse | None:
        """Raises the verification level of one conversation or task. See `Niadra.verify`."""
        if not self._core.enabled:
            return None
        try:
            item = self._core.verify_item(method, level, handle, conversation_id, task_id, valid_until)
        except Exception as exc:
            return self._core.fail("verify", exc, None)
        return await self._send_now("verify", item, conversation_id, task_id)

    async def feedback(
        self,
        action: FeedbackAction,
        subject: HandleLike,
        *,
        fact_id: str | None = None,
        open_item_id: str | None = None,
        conversation_id: str | None = None,
        value: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> BatchResponse | None:
        """Corrects what Niadra derived about a subject. Sent right away. See `Niadra.feedback`."""
        if not self._core.enabled:
            return None
        try:
            body = self._core.feedback_request(
                action, subject, fact_id, open_item_id, conversation_id, value, reason, idempotency_key
            )
            return BatchResponse.model_validate(await self._transport.request(self._core.feedback_http(body)))
        except Exception as exc:
            return self._core.fail("feedback", exc, None)

    async def feedback_batch(
        self, items: Sequence[FeedbackRequest | Mapping[str, Any]]
    ) -> BatchResponse | None:
        """Up to 500 corrections in one call, each with its own `idempotency_key` (one is minted when
        missing): `accepted`, `duplicates` for replayed keys, and one error per refused item."""
        if not self._core.enabled:
            return None
        try:
            requests = [self._core.feedback_item(item) for item in items]
            return BatchResponse.model_validate(
                await self._transport.request(self._core.feedback_batch_http(requests))
            )
        except Exception as exc:
            return self._core.fail("feedback_batch", exc, None)

    async def whoami(self) -> KeyIdentity | None:
        """What this key authenticates as: space, source, vendor, scopes and whether agent memory is on.
        Any key may ask, whatever its scopes; use it to check a key before wiring an agent to it."""
        if not self._core.enabled:
            return None
        try:
            return KeyIdentity.model_validate(await self._transport.request(self._core.whoami_http()))
        except Exception as exc:
            return self._core.fail("whoami", exc, None)

    async def upload_media(
        self, data: bytes | bytearray | memoryview, content_type: str, *, subject: HandleLike | None = None
    ) -> MediaUpload | None:
        """Hands a file to Niadra and returns the reference for its event. See `Niadra.upload_media`."""
        if not self._core.enabled:
            return None
        try:
            payload = bytes(data)
            request, digest = self._core.media_upload_http(payload, content_type, subject)
            reserved = MediaUploadResponse.model_validate(await self._transport.request(request))
            if reserved.upload_url:
                self._core.check_upload_url(reserved.upload_url)
                await self._transport.upload(
                    reserved.upload_url,
                    payload,
                    self._core.upload_headers(reserved, content_type),
                    self._core.timeouts.upload,
                )
            return MediaUpload(
                media_ref=reserved.media_ref,
                media_sha256=digest,
                content_type=content_type,
                size_bytes=len(payload),
                expires_at=reserved.expires_at,
            )
        except Exception as exc:
            return self._core.fail("upload_media", exc, None)

    def handoff(
        self,
        conversation_id: str,
        target: HandoffTarget,
        *,
        target_source: str | None = None,
        reason: str | None = None,
        mode: HandoffMode = "warm",
    ) -> bool:
        """Records a transfer to a human or another agent. Queued."""
        if not self._core.enabled:
            return False
        try:
            item = self._core.handoff_item(conversation_id, target, target_source, reason, mode)
        except Exception as exc:
            return self._core.fail("handoff", exc, False)
        return self.track(item)

    def conversation(
        self,
        conversation_id: str | None = None,
        *,
        subject: HandleLike | None = None,
        object: ObjectLike | None = None,
        about: HandleLike | None = None,
        channel: str | None = None,
        view: str = "chat",
        verification: VerificationLike = Verification.V0,
        target: TargetLike | None = None,
        agent_id: str | None = None,
    ) -> AsyncConversation:
        """A conversation with one customer, as an async context manager. See `Niadra.conversation`."""
        return AsyncConversation(
            self,
            conversation_id,
            subject=subject,
            object=object,
            about=about,
            channel=channel or self._core.channel,
            view=view,
            verification=verification,
            target=target,
            agent_id=agent_id,
        )

    def task(
        self,
        task_id: str | None = None,
        *,
        subject: HandleLike | None = None,
        object: ObjectLike | None = None,
        about: HandleLike | None = None,
        channel: str | None = None,
        view: str = "brief",
        verification: VerificationLike = Verification.V0,
        target: TargetLike | None = None,
        agent_id: str | None = None,
    ) -> AsyncTask:
        """A unit of work of an internal agent, as an async context manager. See `Niadra.task`."""
        return AsyncTask(
            self,
            task_id,
            subject=subject,
            object=object,
            about=about,
            channel=channel or self._core.channel,
            view=view,
            verification=verification,
            target=target,
            agent_id=agent_id,
        )

    def tools(
        self,
        subject: HandleLike,
        *,
        about: HandleLike | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        verification: VerificationLike = Verification.V0,
        voice: bool = False,
        agent_memory: bool = False,
        write_agent_memory: bool = False,
    ) -> AsyncToolKit:
        """The history navigation kit bound to one customer. See `Niadra.tools`."""
        return AsyncToolKit(
            self,
            definitions(agent_memory=agent_memory, write_agent_memory=write_agent_memory),
            subject=subject,
            about=about,
            conversation_id=conversation_id,
            task_id=task_id,
            verification=verification,
            voice=voice,
        )

    async def agent_memory(
        self,
        max_tokens: int = 300,
        *,
        tags: Sequence[str] | None = None,
        view: str | None = None,
        timeout: float | None = None,
    ) -> AgentMemory:
        """The agent's own working notes as one block, for the prompt after your instructions.

        Put `text` after the agent's instructions and before the customer's context: it is the same
        for every customer, so it stays in the cacheable prefix of the prompt. The block is cached
        per `(max_tokens, tags, view)` for the context cache's TTL and revalidated by ETag. With the
        agent memory off in the space, `enabled` is False and `text` empty. Never raises (unless
        `strict`).
        """
        if not self._core.enabled:
            return AgentMemory.empty(error="disabled")
        key = self._core.agent_memory_cache.key(max_tokens, tags, view)
        cached, fresh = self._core.agent_memory_cache.get(key)
        if cached is not None and fresh:
            return cached.model_copy(update={"source": "cache"})
        try:
            budget = self._core.context_budget(view or "chat", timeout)
            etag = cached.etag if cached is not None and cached.etag else None
            request = self._core.agent_memory_http(max_tokens, tags, view, etag, budget)
            return self._core.agent_memory_cache.absorb(key, await self._transport.request(request))
        except Exception as exc:
            return self._core.agent_memory_failed(key, exc)

    async def search_agent_memory(
        self,
        query: str,
        *,
        tags: Sequence[str] | None = None,
        limit: int = 5,
        conversation_id: str | None = None,
        task_id: str | None = None,
        timeout: float | None = None,
    ) -> list[AgentNote]:
        """The agent's notes that match `query` (and `tags`), best first. Empty on failure."""
        if not self._core.enabled:
            return []
        try:
            budget = self._core.navigation_budget(False, timeout)
            request = self._core.search_agent_memory_http(
                query, tags, limit, conversation_id, task_id, budget
            )
            return AgentMemorySearchResponse.model_validate(await self._transport.request(request)).notes
        except Exception as exc:
            return self._core.fail("search_agent_memory", exc, [])

    async def remember(
        self,
        kind: AgentNoteKind,
        title: str,
        body: str,
        *,
        tags: Sequence[str] | None = None,
        evidence: Evidence | Mapping[str, Any] | None = None,
        visibility: AgentNoteVisibility | None = None,
        valid_until: datetime | None = None,
    ) -> RememberResult:
        """Saves a working note: a procedure, how a tool or process behaves, or a pitfall.

        Never about a customer: a note with personal data is refused, and `error` comes back as
        `personal_data_in_agent_memory` (in `strict` mode, an `UnprocessableEntityError` with that
        code). Sent at once, since the answer says whether it was saved or waits for a person
        (`proposal_id`). Needs a key with the `agent_memory:write` scope.
        """
        if not self._core.enabled:
            return RememberResult(error="disabled")
        try:
            request = self._core.remember_http(kind, title, body, tags, evidence, visibility, valid_until)
            return RememberResult.model_validate(await self._transport.request(request))
        except Exception as exc:
            return self._core.fail("remember", exc, RememberResult(error=error_code(exc)))

    async def subject_token(
        self,
        subject: HandleLike,
        *,
        about: HandleLike | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        verification: VerificationLike = Verification.V0,
    ) -> SubjectToken | None:
        """Mints a token that binds an MCP connection to one customer. See `Niadra.subject_token`."""
        if not self._core.enabled:
            return None
        try:
            request = self._core.subject_token_http(subject, about, conversation_id, task_id, verification)
            return SubjectToken.model_validate(await self._transport.request(request))
        except Exception as exc:
            return self._core.fail("subject_token", exc, None)

    async def flush(self, timeout: float | None = None) -> bool:
        """Sends everything queued now. True when nothing is left."""
        if not self._core.enabled:
            return True
        try:
            return await self._flusher.flush(timeout)
        except Exception as exc:
            return self._core.fail("flush", exc, False)

    async def close(self, timeout: float | None = 5.0) -> None:
        """Flushes the queue (for up to `timeout` seconds) and releases connections."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._core.enabled:
                await self._flusher.stop(timeout)
            for task in list(self._refreshes):
                task.cancel()
            await asyncio.gather(*self._refreshes, return_exceptions=True)
            await self._transport.aclose()
        except Exception as exc:
            self._core.fail("close", exc, None)

    async def __aenter__(self) -> AsyncNiadra:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    @property
    def pending(self) -> int:
        """Items waiting in the local queue."""
        return len(self._core.buffer)

    @property
    def dropped(self) -> int:
        """Items dropped so far: queue full, rejected by the API, or unserializable."""
        return self._core.buffer.dropped

    async def _fetch_context(self, request: ContextRequest, budget: float, known_etag: str | None) -> Context:
        started = time.monotonic()
        data = await self._transport.request(self._core.context_http(request, budget, known_etag))
        return self._core.parse_context(data, started)

    def _refresh_later(self, key: str, scope: str, request: ContextRequest, budget: float) -> None:
        if not self._cache.begin_refresh(key):
            return
        task = asyncio.get_running_loop().create_task(self._refresh(key, scope, request, budget))
        self._refreshes.add(task)
        task.add_done_callback(self._refreshes.discard)

    async def _refresh(self, key: str, scope: str, request: ContextRequest, budget: float) -> None:
        try:
            fetched = await self._fetch_context(request, budget, self._cache.etag(key))
            self._cache.absorb(key, scope, fetched, deliver=False)
        except Exception as exc:
            self._cache.drop_on_auth_error(key, exc)
            logger.info("niadra: background context refresh failed (%s)", error_code(exc))
        finally:
            self._cache.end_refresh(key)

    async def _send_batch(self, payloads: list[dict[str, Any]]) -> Any:
        return await self._transport.request(self._core.batch_http(payloads))

    async def _send_now(
        self, method: str, item: BaseModel, conversation_id: str | None, task_id: str | None
    ) -> BatchResponse | None:
        scope = self._core.scope_of(conversation_id, task_id)
        if scope:
            self._cache.purge_scope(scope)
        payload = item.model_dump(mode="json", exclude_none=True)
        try:
            request = self._core.batch_http([payload], budget=self._core.timeouts.write)
            return BatchResponse.model_validate(await self._transport.request(request))
        except Exception as exc:
            if self._core.strict or not is_retryable(exc):
                return self._core.fail(method, exc, None)
            self._core.buffer.put(payload)
            self._flusher.notify()
            logger.warning("niadra: %s could not be sent now, queued for retry (%s)", method, error_code(exc))
            return None
