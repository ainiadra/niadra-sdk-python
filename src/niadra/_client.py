from __future__ import annotations

import atexit
import logging
import threading
import time
import weakref
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import TracebackType
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from niadra._admin import Admin
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
from niadra._queue import SyncFlusher, is_retryable
from niadra._transport import SyncTransport
from niadra._turns import MIN_REREAD
from niadra.conversation import Conversation, Task
from niadra.models.admin import IngestStatus, KeyIdentity
from niadra.models.agent_memory import (
    AgentMemory,
    AgentMemorySearchResponse,
    AgentNote,
    AgentNoteKind,
    AgentNoteVisibility,
    Evidence,
    RememberResult,
)
from niadra.models.context import ContextRequest, HistoryFilters, ObjectState, OpenedItem, PrefetchRequest
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
from niadra.tools import ToolKit, definitions
from niadra.vocabulary import AssertionMethod, Speaker, SubjectKind, Verification


class Niadra:
    """The synchronous Niadra client.

    ```python
    niadra = Niadra()  # reads NIADRA_API_KEY
    context = niadra.context(subject=phone("+5511912345678"), conversation_id=thread_id)
    ```

    The client never takes your agent down with it. Without a key it is a no-op that warns
    once. Every public method catches and logs its own failures and returns a safe value:
    an empty `Context`, an empty `SearchResult`, `False`, `None`. Pass `strict=True` to get
    exceptions instead, which is what you want in tests.

    One instance per process is enough; it is thread-safe. `track()` and friends only queue:
    a background thread sends batches. Call `close()`, or use the client as a context manager,
    to flush before exit; an `atexit` hook also flushes for up to two seconds.

    Args:
        api_key: A source key. Defaults to `NIADRA_API_KEY`.
        base_url: Overrides the address derived from the key, e.g. `http://127.0.0.1:8765`
            for `niadra-mock`. Defaults to `NIADRA_BASE_URL`.
        channel: The default `channel` for events that do not name one, e.g. `"whatsapp"`.
        strict: Raise instead of logging and returning a fallback.
        timeouts: Per-method time budgets.
        cache: The per-conversation context cache.
        queue: Batching of `track()`.
        http_client: An `httpx.Client` to send requests with (proxies, custom transports).
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
        http_client: httpx.Client | None = None,
    ) -> None:
        self._core = ClientCore(
            api_key, base_url, channel=channel, strict=strict, timeouts=timeouts, cache=cache, queue=queue
        )
        self._cache = ContextCache(self._core.cache_options)
        self._transport = SyncTransport(self._core.base_url, self._core.api_key, http_client)
        self.admin = Admin(self._core, self._transport)
        """Governance calls for a key with the `admin` scope: memory, fact history, corrections, erasure."""
        self._flusher = SyncFlusher(self._core.buffer, self._send_batch, self._core.queue_options)
        self._refresher: ThreadPoolExecutor | None = None
        self._prefetcher: ThreadPoolExecutor | None = None
        # Per conversation, one prefetch in flight and the newest text waiting behind it.
        self._prefetching: dict[str, PrefetchRequest | None] = {}
        self._prefetch_lock = threading.Lock()
        self._closed = False
        if self._core.enabled:
            atexit.register(_flush_at_exit, weakref.ref(self))

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

    def context(
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
        turn: str | None = None,
        explain: bool = False,
    ) -> Context:
        """The context pack for a subject (a person, account or partner) or a business object.

        Pass exactly one of `subject` (a handle) or `object` (`"invoice:erp:0823"` or an
        `ObjectRef`). `about` adds what an organization the person acts for has that matters
        here. With a `conversation_id` or `task_id` the server pins the pack to the same bytes
        across turns and the SDK caches it (see `CacheOptions`).

        `target`, e.g. `"openai/gpt-4.1"`, lets the server aim the pack at that model's
        prompt-cache floor. `delta=True` asks for what changed since this agent last looked,
        and is never served from the cache.

        `turn` is the customer's last turn, which a conversation passes for you. In a space with
        memory v2 it goes as `query` and the answer adds `slots`, what the turn selected from
        memory, at the end of `turn_block`, while the pack stays the conversation's pinned one (and
        is cached as without it). A space without memory v2 compiles a read with `query` for it and
        does not pin it, so after one such answer the client stops sending the turn for ten
        minutes. `query` asks for a read focused on that text, as before, and wins over `turn`.

        `explain=True` requires `format="json"` (raises `ValueError` otherwise) and, on a space
        with memory v2, adds `why` to each of `pack.slots`: the retrieval channels that ranked it,
        the fused score, the weights version and, for a derived line, the rule behind it. It
        changes nothing else: the pinned text, the slots chosen and the receipt are the same.

        Never raises (unless `strict`): on failure it returns the last good pack for the same
        key or an empty one. A 401 or 403 also wipes what the cache held for that key.
        """
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
                explain,
            )
        except (TypeError, ValueError) as exc:
            return self._core.fail(
                "context", exc, Context.empty(requested=requested, error="invalid_arguments")
            )
        if not self._core.enabled:
            return Context.empty(requested=requested, error="disabled")
        budget = self._core.context_budget(view, timeout)
        query = self._core.turn_query(request, turn)
        if query is not None:
            return self._turn_context(request, query, budget, use_cache, requested)
        return self._pinned_context(request, budget, use_cache, requested)

    def search(
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
        """Searches the subject's whole history by keyword and meaning.

        Returns items (facts, episodes, actions, objects), a `recurrence`
        count when the query matches a category, and `withheld`, the number of items the
        policy held back at this verification level. `voice=True` uses the shorter voice budget.
        """
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
            return SearchResult.model_validate(self._transport.request(request))
        except Exception as exc:
            return self._core.fail("search", exc, SearchResult(error=error_code(exc)))

    def timeline(
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
            return TimelinePage.model_validate(self._transport.request(request))
        except Exception as exc:
            return self._core.fail("timeline", exc, TimelinePage(error=error_code(exc)))

    def open(
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
        """Opens an episode or object found by `search()` or `timeline()`. None when unavailable.

        Sent as `POST /v1/history/open`: the conversation id and `subject` go in the body, never in a
        URL. With `subject`, the server opens the item only when it belongs to that customer, which
        is how `tools()` keeps the model on the bound customer. `task_id` is accepted for
        compatibility; the server scopes opening by conversation only.
        """
        if not self._core.enabled:
            return None
        try:
            budget = self._core.navigation_budget(voice, timeout)
            request = self._core.open_http(item_id, verification, conversation_id, subject, budget)
            return OpenedItem.model_validate(self._transport.request(request))
        except Exception as exc:
            return self._core.fail("open", exc, None)

    def object_state(
        self, object: ObjectLike, *, voice: bool = False, timeout: float | None = None
    ) -> ObjectState | None:
        """The derived state of a business object, e.g. `object_state("invoice:erp:0823")`.

        What its systems of record reported last, `as_of` when, and its open items, under this
        source's purpose. None when unavailable.
        """
        if not self._core.enabled:
            return None
        try:
            request = self._core.object_http(object, voice, timeout)
            return ObjectState.model_validate(self._transport.request(request))
        except Exception as exc:
            return self._core.fail("object_state", exc, None)

    def object_timeline(
        self,
        object: ObjectLike,
        *,
        cursor: str | None = None,
        limit: int = 20,
        voice: bool = False,
        timeout: float | None = None,
    ) -> ObjectTimeline | None:
        """System events and agent actions about one object, newest first. Pass `next_cursor` to go on.

        One line per item, never conversation content. None when unavailable.
        """
        if not self._core.enabled:
            return None
        try:
            request = self._core.object_timeline_http(object, cursor, limit, voice, timeout)
            return ObjectTimeline.model_validate(self._transport.request(request))
        except Exception as exc:
            return self._core.fail("object_timeline", exc, None)

    def track(self, item: ItemLike) -> bool:
        """Queues a message, system event or action (or any other batch item) and returns at once.

        Accepts an `EventItem` or a mapping of its fields; a mapping without `channel` gets
        the client's default. Returns False when the item was dropped: invalid, unserializable,
        the queue is full, or the client is disabled.
        """
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
        """Records what an agent did in a system of record: `action("credit", object="invoice:erp:0823")`.

        `closes` names the open item the action fulfils, by id (a string) or as
        `{"object": ..., "operation": ...}`. The action stays `declared` until the system
        of record confirms it with its own event. `context_stamp` says which context the
        agent acted on; conversations and tasks set it for you.
        """
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

    def identify(
        self,
        handles: Sequence[HandleLike],
        *,
        method: AssertionMethod | str = AssertionMethod.EXPLICIT_IDENTIFY,
        subject_kind: SubjectKind | str = SubjectKind.PERSON,
        conversation_id: str | None = None,
    ) -> BatchResponse | None:
        """States that two or more handles belong to the same subject.

        Sent right away rather than queued, so a `context()` call that follows sees it. If
        the request fails, the assertion is queued for the background sender and None is returned.
        """
        if not self._core.enabled:
            return None
        try:
            item = self._core.identify_item(handles, method, subject_kind, conversation_id)
        except Exception as exc:
            return self._core.fail("identify", exc, None)
        return self._send_now("identify", item, conversation_id, None)

    def verify(
        self,
        method: VerifyMethod,
        level: VerificationLike,
        *,
        handle: HandleLike,
        conversation_id: str | None = None,
        task_id: str | None = None,
        valid_until: datetime | None = None,
    ) -> BatchResponse | None:
        """Raises the verification level of one conversation or task, after you proved it.

        `method` is how: `otp_whatsapp`, `otp_sms`, `login`, `kba`, `network_attestation` or
        `human_agent`. Sent right away, and the cached pack of that conversation is dropped,
        so the next `context()` reflects the new level.
        """
        if not self._core.enabled:
            return None
        try:
            item = self._core.verify_item(method, level, handle, conversation_id, task_id, valid_until)
        except Exception as exc:
            return self._core.fail("verify", exc, None)
        return self._send_now("verify", item, conversation_id, task_id)

    def feedback(
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
        """Corrects what Niadra derived about a subject.

        `action` is `retract_fact` or `correct_fact` (with `fact_id`, and `value` for the right
        one), `resolve_open_item` (with `open_item_id`) or `conversation_outcome` (with
        `conversation_id` and `value`). The server records the correction as an event, so it is
        audited like any other. Sent right away; None when it could not be delivered.
        """
        if not self._core.enabled:
            return None
        try:
            body = self._core.feedback_request(
                action, subject, fact_id, open_item_id, conversation_id, value, reason, idempotency_key
            )
            return BatchResponse.model_validate(self._transport.request(self._core.feedback_http(body)))
        except Exception as exc:
            return self._core.fail("feedback", exc, None)

    def feedback_batch(self, items: Sequence[FeedbackRequest | Mapping[str, Any]]) -> BatchResponse | None:
        """Up to 500 corrections in one call, each with its own `idempotency_key` (one is minted when
        missing): `accepted`, `duplicates` for replayed keys, and one error per refused item."""
        if not self._core.enabled:
            return None
        try:
            requests = [self._core.feedback_item(item) for item in items]
            return BatchResponse.model_validate(
                self._transport.request(self._core.feedback_batch_http(requests))
            )
        except Exception as exc:
            return self._core.fail("feedback_batch", exc, None)

    def ingest_status(
        self, *, conversation_id: str | None = None, task_id: str | None = None
    ) -> IngestStatus | None:
        """Whether what was sent for a conversation or task became memory yet: `open` (still
        receiving turns), `processing`, `ready` (memory applies it in seconds), `failed` or `unknown`.
        States and times only. Flush the queue first if the turns were sent with `track()`."""
        if not self._core.enabled:
            return None
        try:
            request = self._core.ingest_status_http(conversation_id, task_id)
            return IngestStatus.model_validate(self._transport.request(request))
        except Exception as exc:
            return self._core.fail("ingest_status", exc, None)

    def whoami(self) -> KeyIdentity | None:
        """What this key authenticates as: space, source, vendor, scopes and whether agent memory is on.
        Any key may ask, whatever its scopes; use it to check a key before wiring an agent to it."""
        if not self._core.enabled:
            return None
        try:
            return KeyIdentity.model_validate(self._transport.request(self._core.whoami_http()))
        except Exception as exc:
            return self._core.fail("whoami", exc, None)

    def upload_media(
        self, data: bytes | bytearray | memoryview, content_type: str, *, subject: HandleLike | None = None
    ) -> MediaUpload | None:
        """Hands a file to Niadra, such as a call recording, and returns the reference for its event.

        Media never travels inside an event. This reserves an upload, sends the bytes straight
        to storage over a short-lived signed URL, and returns `media_ref` and `media_sha256`
        for the event's `content`. Pass the `subject` it belongs to whenever you know it: the
        file is then stored under that person, so erasing them erases it, even if no event ever
        references it. None when the upload failed.
        """
        if not self._core.enabled:
            return None
        try:
            payload = bytes(data)
            request, digest = self._core.media_upload_http(payload, content_type, subject)
            reserved = MediaUploadResponse.model_validate(self._transport.request(request))
            if reserved.upload_url:
                self._core.check_upload_url(reserved.upload_url)
                self._transport.upload(
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
        """Records a transfer to a human (`"human"`) or another agent (`"agent"`). Queued."""
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
    ) -> Conversation:
        """A conversation with one customer. Use it as a context manager; see `niadra.conversation`.

        Without a `conversation_id` the SDK mints one. `agent_id` identifies your agent
        within the source, and is stamped on its turns and actions.
        """
        return Conversation(
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
    ) -> Task:
        """A unit of work of an internal agent (billing, orders, tickets). Emits `task.ended` on exit.

        With an `object`, the pack is centered on it; use a task view such as `"task:billing"`.
        """
        return Task(
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
    ) -> ToolKit:
        """The history navigation kit as function-calling tools, bound to one customer.

        The customer is bound here, outside the model's reach: the model picks the query,
        never the profile, which is what stops a prompt injection from switching customers.
        Hand `toolkit.definitions` to your model and route its tool calls to `toolkit.call()`.
        """
        return ToolKit(
            self,
            definitions(agent_memory=agent_memory, write_agent_memory=write_agent_memory),
            subject=subject,
            about=about,
            conversation_id=conversation_id,
            task_id=task_id,
            verification=verification,
            voice=voice,
        )

    def agent_memory(
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
            return self._core.agent_memory_cache.absorb(key, self._transport.request(request))
        except Exception as exc:
            return self._core.agent_memory_failed(key, exc)

    def search_agent_memory(
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
            return AgentMemorySearchResponse.model_validate(self._transport.request(request)).notes
        except Exception as exc:
            return self._core.fail("search_agent_memory", exc, [])

    def remember(
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
            return RememberResult.model_validate(self._transport.request(request))
        except Exception as exc:
            return self._core.fail("remember", exc, RememberResult(error=error_code(exc)))

    def subject_token(
        self,
        subject: HandleLike,
        *,
        about: HandleLike | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        verification: VerificationLike = Verification.V0,
    ) -> SubjectToken | None:
        """Mints a signed, 15-minute token that binds an MCP connection to one customer.

        Call it from your backend and pass `token.headers` when your agent opens its MCP
        connection to `mcp_url`. None when the token could not be minted.
        """
        if not self._core.enabled:
            return None
        try:
            request = self._core.subject_token_http(subject, about, conversation_id, task_id, verification)
            return SubjectToken.model_validate(self._transport.request(request))
        except Exception as exc:
            return self._core.fail("subject_token", exc, None)

    def prefetch(
        self,
        subject: HandleLike | None = None,
        object: ObjectLike | None = None,
        *,
        text: str,
        about: HandleLike | None = None,
        view: str = "voice",
        verification: VerificationLike = Verification.V0,
        conversation_id: str | None = None,
        task_id: str | None = None,
    ) -> bool:
        """Sends a partial transcript of the customer's turn while they are still speaking.

        The server reads it the way it will read the final turn and warms what that read needs
        (memory v2), so the `context()` that answers the turn spends less of its budget. It runs
        in the background: it returns at once, never raises and never holds a turn. True when it
        was sent or queued: while one runs for the same conversation, the newest text waits and
        goes when it ends, and older waiting texts are dropped. False when there was nothing worth
        sending (blank or very short text) or the space does not read turns.
        """
        if not self._core.enabled or self._closed:
            return False
        try:
            request = self._core.prefetch_request(
                subject, object, about, view, verification, conversation_id, task_id, text
            )
        except (TypeError, ValueError):
            return False
        scope = self._core.scope_of(conversation_id, task_id)
        if request is None:
            return False
        with self._prefetch_lock:
            if scope in self._prefetching:
                self._prefetching[scope] = request  # the newest text waits for the one in flight
                return True
            self._prefetching[scope] = None
        if self._prefetcher is None:
            self._prefetcher = ThreadPoolExecutor(max_workers=2, thread_name_prefix="niadra-prefetch")
        try:
            self._prefetcher.submit(self._prefetch, scope, request)
        except RuntimeError:
            with self._prefetch_lock:
                self._prefetching.pop(scope, None)  # the executor is shutting down
            return False
        return True

    def _prefetch(self, scope: str, request: PrefetchRequest | None) -> None:
        while request is not None:
            try:
                self._transport.request(self._core.prefetch_http(request))
            except Exception as exc:
                self._core.prefetch_failed(exc)
            with self._prefetch_lock:
                request = self._prefetching.pop(scope, None)
                if request is not None and self._core.turns.prefetch_wanted():
                    self._prefetching[scope] = None
                else:
                    request = None

    def flush(self, timeout: float | None = None) -> bool:
        """Sends everything queued, from the calling thread. True when nothing is left."""
        if not self._core.enabled:
            return True
        try:
            return self._flusher.flush(timeout)
        except Exception as exc:
            return self._core.fail("flush", exc, False)

    def close(self, timeout: float | None = 5.0) -> None:
        """Flushes the queue (for up to `timeout` seconds) and releases connections."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._core.enabled:
                self._flusher.stop(timeout)
            if self._refresher is not None:
                self._refresher.shutdown(wait=True)  # refreshes are bounded by the context budget
            if self._prefetcher is not None:
                self._prefetcher.shutdown(wait=True)  # bounded by the prefetch budget
            self._transport.close()
        except Exception as exc:
            self._core.fail("close", exc, None)

    def __enter__(self) -> Niadra:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    @property
    def pending(self) -> int:
        """Items waiting in the local queue."""
        return len(self._core.buffer)

    @property
    def dropped(self) -> int:
        """Items dropped so far: queue full, rejected by the API, or unserializable."""
        return self._core.buffer.dropped

    def _fetch_context(self, request: ContextRequest, budget: float, known_etag: str | None) -> Context:
        started = time.monotonic()
        data = self._transport.request(self._core.context_http(request, budget, known_etag))
        return self._core.parse_context(data, started)

    def _pinned_context(
        self, request: ContextRequest, budget: float, use_cache: bool, requested: Verification
    ) -> Context:
        """A read without the turn: the conversation's pinned pack, from the cache when it is fresh."""
        if not self._core.cacheable(request, use_cache):
            try:
                return self._fetch_context(request, budget, None)
            except Exception as exc:
                return self._core.fail(
                    "context", exc, Context.empty(requested=requested, error=error_code(exc))
                )

        key = cache_key(request)
        scope = self._core.scope_of(request.conversation_id, request.task_id)
        hit = self._cache.get(key)
        if hit is not None and hit.freshness == "fresh":
            return hit.context
        if hit is not None and hit.freshness == "stale":
            self._refresh_later(key, scope, request, budget)
            return hit.context
        try:
            fetched = self._fetch_context(request, budget, self._cache.etag(key))
            return self._cache.absorb(key, scope, fetched)
        except Exception as exc:
            fallback = self._cache.fallback(key, exc)
            if fallback is not None and not self._core.strict:
                logger.warning("niadra: context failed, serving the last good pack (%s)", error_code(exc))
                return fallback
            return self._core.fail("context", exc, Context.empty(requested=requested, error=error_code(exc)))

    def _turn_context(
        self, request: ContextRequest, query: str, budget: float, use_cache: bool, requested: Verification
    ) -> Context:
        """A read that sends the customer's turn: always asked of the API, since the slots are this
        turn's; the pack is cached, and served on failure, as the read without `query`."""
        cacheable = self._core.cacheable(request, use_cache)
        key = cache_key(request) if cacheable else None
        scope = self._core.scope_of(request.conversation_id, request.task_id)
        # A `not_modified` answer carries no `pack`, so a read as data asks for the whole answer.
        known = self._cache.etag(key) if key is not None and request.format != "json" else None
        started = time.monotonic()
        try:
            fetched = self._fetch_context(request.model_copy(update={"query": query}), budget, known)
        except Exception as exc:
            fallback = self._cache.fallback(key, exc) if key is not None else None
            if fallback is not None and not self._core.strict:
                logger.warning("niadra: context failed, serving the last good pack (%s)", error_code(exc))
                return fallback
            return self._core.fail("context", exc, Context.empty(requested=requested, error=error_code(exc)))
        if self._core.turns.observe(fetched) is False and not fetched.not_modified:
            # This space compiled the pack for the turn and did not pin it: the conversation reads its
            # pinned pack as before, within what is left of the budget, and stops sending the turn.
            left = budget - (time.monotonic() - started)
            if left >= MIN_REREAD:
                return self._pinned_context(request, left, use_cache, requested)
            return self._core.unpinned(fetched)
        return self._core.settle_turn(self._cache, key, scope, fetched)

    def _refresh_later(self, key: str, scope: str, request: ContextRequest, budget: float) -> None:
        if not self._cache.begin_refresh(key):
            return
        if self._refresher is None:
            self._refresher = ThreadPoolExecutor(max_workers=2, thread_name_prefix="niadra-refresh")
        try:
            self._refresher.submit(self._refresh, key, scope, request, budget)
        except RuntimeError:
            self._cache.end_refresh(key)  # the executor is shutting down

    def _refresh(self, key: str, scope: str, request: ContextRequest, budget: float) -> None:
        try:
            fetched = self._fetch_context(request, budget, self._cache.etag(key))
            self._cache.absorb(key, scope, fetched, deliver=False)
        except Exception as exc:
            self._cache.drop_on_auth_error(key, exc)
            logger.info("niadra: background context refresh failed (%s)", error_code(exc))
        finally:
            self._cache.end_refresh(key)

    def _send_batch(self, payloads: list[dict[str, Any]]) -> Any:
        return self._transport.request(self._core.batch_http(payloads))

    def _send_now(
        self, method: str, item: BaseModel, conversation_id: str | None, task_id: str | None
    ) -> BatchResponse | None:
        scope = self._core.scope_of(conversation_id, task_id)
        if scope:
            self._cache.purge_scope(scope)
        payload = item.model_dump(mode="json", exclude_none=True)
        try:
            request = self._core.batch_http([payload], budget=self._core.timeouts.write)
            return BatchResponse.model_validate(self._transport.request(request))
        except Exception as exc:
            if self._core.strict or not is_retryable(exc):
                return self._core.fail(method, exc, None)
            self._core.buffer.put(payload)
            self._flusher.notify()
            logger.warning("niadra: %s could not be sent now, queued for retry (%s)", method, error_code(exc))
            return None


def _flush_at_exit(ref: weakref.ref[Niadra]) -> None:
    client = ref()
    if client is not None and not client._closed:
        try:
            client.close(timeout=2.0)
        except Exception:
            logging.getLogger("niadra").debug("niadra: flush at exit failed", exc_info=True)
