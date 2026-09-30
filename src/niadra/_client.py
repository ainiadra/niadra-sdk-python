from __future__ import annotations

import atexit
import contextlib
import logging
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from contextlib import suppress
from datetime import datetime
from types import TracebackType
from typing import Any, Literal, TypeVar
from uuid import UUID

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
    as_handle,
    error_code,
    logger,
)
from niadra._cache import ContextCache, cache_key
from niadra._outbox import SyncOutbox
from niadra._profile import ProfileCache
from niadra._queue import SyncFlusher, is_retryable
from niadra._transport import SyncTransport
from niadra._voice import TurnRead, VoiceLine, VoiceLines, compose, words_of
from niadra.agent_state import AgentStates
from niadra.api import Api
from niadra.claims.internal import InternalText
from niadra.content import ContentResolver
from niadra.conversation import Conversation, Task
from niadra.coordination.client import Coordinator
from niadra.coordination.suppression import FAIL_OPEN, MAX_PAGES, SuppressionCopy
from niadra.coordination.token import ContactGateway, SeenTokens
from niadra.errors import APITimeoutError
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
from niadra.models.context import ContextRequest, HistoryFilters, OpenedItem, PrefetchRequest
from niadra.models.events import (
    BatchResponse,
    FeedbackAction,
    FeedbackRequest,
    MediaUploadResponse,
    VerifyMethod,
)
from niadra.models.objects import ObjectTimeline
from niadra.models.results import Context, MediaUpload, SearchResult, TimelinePage
from niadra.models.state import ClaimContractSummary, ObjectRead, SdkProfile, StateRef
from niadra.models.tokens import SubjectToken
from niadra.models.turns import TurnPins
from niadra.options import CacheOptions, QueueOptions, Timeouts, TurnOptions, VoiceOptions
from niadra.replay.playback import replaying
from niadra.resolvers import (
    CLAIM_BUDGET,
    ClaimVerdict,
    Resolvers,
    as_ref,
    from_niadra,
    from_resolver,
    verdict_of,
    verify_http,
)
from niadra.tools import ToolKit, definitions
from niadra.turns.capture import TurnFrame
from niadra.turns.claims import check_turn
from niadra.turns.recorder import TurnRecorder
from niadra.turns.sender import SyncTurnSender
from niadra.turns.tool import BoundTool
from niadra.vocabulary import AssertionMethod, Speaker, SubjectKind, Verification

F = TypeVar("F", bound=Callable[..., Any])


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
        voice: The voice read path of conversations in the `voice` view (`niadra._voice`).
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
        voice: VoiceOptions | None = None,
        turns: TurnOptions | None = None,
    ) -> None:
        self._core = ClientCore(
            api_key,
            base_url,
            channel=channel,
            strict=strict,
            timeouts=timeouts,
            cache=cache,
            queue=queue,
            voice=voice,
        )
        self._cache = ContextCache(self._core.cache_options)
        self._transport = SyncTransport(self._core.base_url, self._core.api_key, http_client)
        self.admin = Admin(self._core, self._transport)
        """Governance calls for a key with the `admin` scope: memory, fact history, corrections, erasure."""
        self.api = Api(self._core, self._transport)
        """The routes of turn records, typed state, signals and coordination, one method each. They raise."""
        self.turns = TurnRecorder(turns or TurnOptions(), enabled=self._core.enabled)
        """Turn records: `conversation.turn()` opens one, and a background sender posts the closed ones."""
        self._profile = ProfileCache()
        self._suppressions = SuppressionCopy()
        self._suppressions_lock = threading.Lock()
        self._suppressions_reading = False
        self._outbox = SyncOutbox(self._transport.request)
        self._coordination = Coordinator(self._outbox, self._suppressions)
        self._agent_states = AgentStates(self._outbox)
        self.resolvers = Resolvers()
        """The company's resolvers by object type: `resolvers.register(type, fn)` (`niadra.resolvers`)."""
        self.content = ContentResolver()
        """The company's content resolver, for content kept by pointer: `content.register(fetch)`."""
        self.turns.features = lambda: self._profile.features
        self.turns.recording_mode = self._profile.recording_mode
        self.turns.required_pins = self._profile.required_pins
        self.turns.families = self._profile.families
        self.turns.bindings = self._profile.tool_binding
        self.turns.field_access = self._profile.field_access
        self.turns.claims = self._claims_of
        self.internal_text = InternalText()
        """Fingerprints of the company's own prompt, by version:
        `internal_text.register("prompts@v16", text)`. An output that repeats a passage of it gives way to the
        claim contract's line (`niadra.claims.internal`); the prompt never leaves the process."""
        self.turns.sender = SyncTurnSender(
            self.turns,
            self.turns.queue,
            self.turns.interval,
            self._transport.request,
            self._core.timeouts.write,
            refresh=self._refresh_profile,
        )
        self._flusher = SyncFlusher(self._core.buffer, self._send_batch, self._core.queue_options)
        self._refresher: ThreadPoolExecutor | None = None
        self._prefetcher: ThreadPoolExecutor | None = None
        # Per conversation, one prefetch in flight and the newest text waiting behind it.
        self._prefetching: dict[str, PrefetchRequest | None] = {}
        self._prefetch_lock = threading.Lock()
        self._voice = VoiceLines(self._core.voice_options)
        self._voice_workers: ThreadPoolExecutor | None = None
        self._closed = False
        if self._core.enabled:
            atexit.register(_flush_at_exit, weakref.ref(self))

    @staticmethod
    def build(
        *,
        prompts: Mapping[str, str] | None = None,
        corpus_digest: str | None = None,
        model: str | None = None,
        assembler: str | None = None,
        tool_schemas: Mapping[str, str] | None = None,
    ) -> TurnPins:
        """The build a turn runs on, for `conversation.turn(build=...)`: each prompt's version, the digest of
        the files the agent consults (`sha256:<hex>`, computed by you, never the files), the exact model,
        your context assembler's version and each tool schema's digest. A replay needs the ones the space's
        recording requires; the pack's hash and compiler are filled in by the SDK."""
        return TurnPins(
            prompts=dict(prompts or {}),
            corpus_digest=corpus_digest,
            model=model,
            assembler=assembler,
            tool_schemas=dict(tool_schemas or {}),
        )

    tool = BoundTool()
    """A decorator that records each call of a tool of yours in the turn it runs in: arguments, result,
    latency and failure, and with `provenance` the objects the result showed. Outside a turn the tool runs
    untouched. `dry_run=True` lets a replay run it for real when the record has no answer; the binding the SDK
    profile serves for its name measures the constraints block against its calls; `mask_output=True` keeps the
    fields this key may not read from the model, by this client's SDK profile, and left unset the served
    binding decides. See `niadra.turns.tool`."""

    def profile(self, *, timeout: float | None = None) -> SdkProfile | None:
        """The SDK profile of this key's space: the features it turned on, the claim contract, the
        summarized type registry and this source's tool bindings, from the local cache, read again once
        `valid_for_s` has passed. When Niadra does not answer, the last profile read stays in use; None when
        there is none yet. Never raises unless `strict`."""
        if self._core.enabled and self._profile.due():
            budget = timeout if timeout is not None else self._core.timeouts.navigation
            try:
                self._profile.absorb(self._transport.request(self._profile.request(budget)))
            except Exception as exc:
                self._core.fail("profile", exc, None)
        return self._profile.profile

    def use_claim_contract(self, contract: ClaimContractSummary | Mapping[str, Any] | None) -> None:
        """Checks outputs against this claim contract instead of the one the profile serves (a company's
        own copy, in CI or a local run); None goes back to the profile's."""
        self._profile.claim_contract = (
            None if contract is None else ClaimContractSummary.model_validate(contract)
        )

    def _claims_of(self, frame: TurnFrame) -> list[dict[str, Any]]:
        contract = self._profile.contract()
        return check_turn(frame, contract, self.internal_text) if contract is not None else []

    def _refresh_profile(self) -> None:
        with contextlib.suppress(Exception):
            self.profile()

    def _contract(self) -> ClaimContractSummary | None:
        """The claim contract in force, the profile read first when it is due."""
        self._refresh_profile()
        return self._profile.contract()

    def verify_claim(
        self,
        ref: StateRef | Mapping[str, Any] | str,
        field: str,
        value: Any,
        *,
        subject: HandleLike | None = None,
        budget: float | None = None,
    ) -> ClaimVerdict:
        """Whether `value` may be claimed for `field` of the object `ref` (`type:namespace:id`) now: Niadra's
        verdict, and when that is not safe, a fresh read by the company's resolver of the type, all within
        `budget` (300 ms). Never a stale value as verified; never raises (`niadra.resolvers`)."""
        target = as_ref(ref)
        seconds = budget if budget is not None else CLAIM_BUDGET
        deadline = time.monotonic() + seconds
        verdict = None
        if self._core.enabled:
            try:
                who = as_handle(subject) if subject is not None else None
                verdict = verdict_of(self._transport.request(verify_http(target, field, value, who, seconds)))
            except Exception:
                verdict = None
        if verdict is not None and (verdict.claim_safe or not self.resolvers.available(target.type)):
            return from_niadra(verdict)
        left = deadline - time.monotonic()
        resolved = self.resolvers.resolve(target, [field], left) if left > 0 else None
        return from_resolver(target, field, value, resolved, verdict)

    def contact_gateway(
        self, gateway_id: str, *, space: str | UUID, key: str | bytes, seen: SeenTokens | None = None
    ) -> ContactGateway:
        """The contact token's offline check for a gateway of yours (`niadra.coordination.token`): its id, the
        space's id and the key it shares with Niadra. The space's public keys are read through this client
        and kept; `seen` shares the tokens let through between processes."""
        return ContactGateway(
            gateway_id, space=space, key=key, read=lambda: self.api.contact_keys(space=str(space)), seen=seen
        )

    def may_contact(
        self,
        handle: HandleLike,
        purpose: str,
        *,
        channel: str | None = None,
        fail_open: bool | None = None,
    ) -> bool:
        """Whether an outbound contact of `purpose` (`marketing`, `service`...) to `handle` may go, by the
        local copy of the space's suppression list: the opt-out holds with Niadra down, from the last copy
        read. Only messages the agent starts need it; an answer to the customer is never suppressed.

        The copy is read on the first call (within `Timeouts.navigation`) and again in the background once a
        minute. With no copy and Niadra out of reach, the purpose decides: `transactional` and `service`
        go, every other purpose waits; `fail_open` overrides that. A space without a list suppresses
        nothing. Never raises unless `strict`."""
        try:
            target = as_handle(handle).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            return self._core.fail(
                "may_contact", exc, fail_open if fail_open is not None else purpose in FAIL_OPEN
            )
        if self._core.enabled and self._suppressions.due():
            if self._suppressions.held:
                self._read_suppressions_later()
            else:
                self._read_suppressions(self._core.timeouts.navigation)
        return self._suppressions.may_contact(target, purpose, channel=channel, fail_open=fail_open)

    def _keep_suppressions(self) -> None:
        """Reads the local copy of the suppression list in the background when it is due: a check that Niadra
        does not answer falls back on it, so an opt-out holds through an outage."""
        if self._core.enabled and self._suppressions.due():
            self._read_suppressions_later()

    def _read_suppressions(self, budget: float) -> None:
        copy, deadline = self._suppressions, time.monotonic() + budget
        try:
            for _ in range(MAX_PAGES):
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                if copy.needs_salt():
                    copy.take_salt(self._transport.request(copy.salt_request(left)))
                    continue
                if not copy.apply(self._transport.request(copy.page_request(left))):
                    return
        except Exception as exc:
            copy.failed(exc)

    def _read_suppressions_later(self) -> None:
        with self._suppressions_lock:
            if self._suppressions_reading:
                return
            self._suppressions_reading = True

        def read() -> None:
            try:
                self._read_suppressions(self._core.timeouts.write)
            finally:
                with self._suppressions_lock:
                    self._suppressions_reading = False

        threading.Thread(target=read, name="niadra-suppressions", daemon=True).start()

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
        include: Sequence[str] | None = None,
    ) -> Context:
        """The context pack for a subject (a person, account or partner) or a business object.

        Pass exactly one of `subject` (a handle) or `object` (`"invoice:erp:0823"` or an
        `ObjectRef`). `about` adds what an organization the person acts for has that matters
        here. With a `conversation_id` or `task_id` the server pins the pack to the same bytes
        across turns and the SDK caches it (see `CacheOptions`).

        `target`, e.g. `"openai/gpt-4.1"`, lets the server aim the pack at that model's
        prompt-cache floor. `delta=True` asks for what changed since this agent last looked,
        and is never served from the cache.

        `turn` is the customer's last turn, which a conversation passes for you. It goes as `query`
        and the answer adds `slots`, what the turn selected from memory, at the end of
        `turn_block`, while the pack stays the conversation's pinned one (and is cached as without
        it). `query` selects the slots by other words and wins over `turn`; it never changes the
        pack either.

        `explain=True` requires `format="json"` (raises `ValueError` otherwise) and adds `why` to
        each of `pack.slots`: the retrieval channels that ranked it, the fused score, the weights
        version and, for a derived line, the rule behind it. It changes nothing else: the pinned
        text, the slots chosen and the receipt are the same.

        `include` adds blocks read in the same round trip: `["constraints"]` returns the subject's
        constraints block in `constraints`, `["state"]` their typed objects in `state`. A block the space
        does not serve is left out (and not asked for again for 10 minutes); the rest of the read is the
        same. The blocks are cached with the pack, so a read that fails serves the last good ones too.

        Never raises (unless `strict`): on failure it returns the last good pack for the same
        key or an empty one. A 401 or 403 also wipes what the cache held for that key.
        """
        requested = Verification.V0
        if (played := replaying()) is not None:
            return played.context or Context.empty(requested=requested, error="replay")
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
                include,
            )
        except (TypeError, ValueError) as exc:
            return self._core.fail(
                "context", exc, Context.empty(requested=requested, error="invalid_arguments")
            )
        if not self._core.enabled:
            return Context.empty(requested=requested, error="disabled")
        budget = self._core.context_budget(view, timeout)
        query = self._core.turn_query(request, turn)
        request = request.model_copy(update={"query": None})  # the pack's key is the read's without it
        if self._core.voice_path(request, use_cache):
            result = self._voice_context(request, query, budget, requested)
        elif query is not None:
            result = self._turn_context(request, query, budget, use_cache, requested)
        else:
            result = self._pinned_context(request, budget, use_cache, requested)
        if self.content.registered and result.state is not None:
            return self.content.fill(result)
        return result

    def begin(
        self,
        subject: HandleLike | None = None,
        object: ObjectLike | None = None,
        *,
        about: HandleLike | None = None,
        view: str = "voice",
        verification: VerificationLike = Verification.V0,
        conversation_id: str | None = None,
        task_id: str | None = None,
        target: TargetLike | None = None,
        format: Literal["text", "json"] = "text",
    ) -> bool:
        """Starts a voice conversation's first read now, in the background: call it when the call starts
        (ringing, the inbound webhook, the caller joining), so the read runs while the call is set up.

        It is bounded by `Timeouts.context_voice_start`. The `context()` calls that follow, with the
        same arguments, take its pack instead of starting their own read; a turn that finds it still
        running waits for it only within its own budget. True when the read runs or its pack is
        already here; False outside the voice read path (another view, no conversation or task id,
        the cache or `VoiceOptions.enabled` off). Never raises (unless `strict`).
        """
        try:
            request = self._core.context_request(
                subject,
                object,
                about,
                view,
                verification,
                conversation_id,
                task_id,
                None,
                False,
                target,
                format,
            )
        except (TypeError, ValueError) as exc:
            return self._core.fail("begin", exc, False)
        if not self._core.enabled or self._closed or not self._core.voice_path(request, True):
            return False
        return self._voice_begin(request)

    @property
    def rtt(self) -> float | None:
        """The round trip to the region in seconds, measured once with the first voice read; None
        before that, or when `VoiceOptions.probe` is off."""
        return self._voice.rtt

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
        subject: HandleLike | None = None,
        voice: bool = False,
        timeout: float | None = None,
    ) -> OpenedItem | None:
        """Opens an episode or object found by `search()` or `timeline()`. None when unavailable.

        Sent as `POST /v1/history/open`: the conversation id and `subject` go in the body, never in a
        URL. With `subject`, the server opens the item only when it belongs to that customer, which
        is how `tools()` keeps the model on the bound customer.
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
    ) -> ObjectRead | None:
        """A business object as a `display` state read serves it, e.g. `object_state("invoice:erp:0823")`:
        each field its systems of record reported, with its logical value, stamps and freshness, under this
        source's purpose (the object state spec). A type the space does not declare reads as one with no
        rules. None when unavailable.
        """
        if not self._core.enabled:
            return None
        try:
            request = self._core.object_http(object, voice, timeout)
            return ObjectRead.model_validate(self._transport.request(request))
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
        the queue is full, or the client is disabled. Inside a replayed turn nothing leaves.
        """
        if (played := replaying()) is not None:
            return played.muted(item)
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
        so the `context()` that answers the turn spends less of its budget. It runs
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
        self._voice_heard(scope, request.query)
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
                if request is not None:
                    self._prefetching[scope] = None
                else:
                    request = None

    def flush(self, timeout: float | None = None) -> bool:
        """Sends everything queued, from the calling thread. True when nothing is left."""
        if not self._core.enabled:
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            events = self._flusher.flush(timeout)
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            turns = self._turn_sender().flush(left)
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            return self._outbox.flush(left) and turns and events
        except Exception as exc:
            return self._core.fail("flush", exc, False)

    def close(self, timeout: float | None = 5.0) -> None:
        """Flushes the queue (for up to `timeout` seconds) and releases connections."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._core.enabled:
                deadline = None if timeout is None else time.monotonic() + timeout
                self._flusher.stop(timeout)
                self._turn_sender().stop(None if deadline is None else max(0.0, deadline - time.monotonic()))
                self._outbox.stop(None if deadline is None else max(0.0, deadline - time.monotonic()))
            if self._refresher is not None:
                self._refresher.shutdown(wait=True)  # refreshes are bounded by the context budget
            if self._prefetcher is not None:
                self._prefetcher.shutdown(wait=True)  # bounded by the prefetch budget
            self._voice.clear()  # settling partials stop at their next check
            if self._voice_workers is not None:
                self._voice_workers.shutdown(wait=True)  # reads bounded by their budgets, settles by `settle`
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
        try:
            data = self._transport.request(self._core.context_http(request, budget, known_etag))
        except Exception as exc:
            plain = self._core.refused_blocks(request, exc)
            left = budget - (time.monotonic() - started)
            if plain is None or left <= 0:
                raise
            data = self._transport.request(self._core.context_http(plain, left, known_etag))
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
        try:
            fetched = self._fetch_context(request.model_copy(update={"query": query}), budget, known)
        except Exception as exc:
            fallback = self._cache.fallback(key, exc) if key is not None else None
            if fallback is not None and not self._core.strict:
                logger.warning("niadra: context failed, serving the last good pack (%s)", error_code(exc))
                return fallback
            return self._core.fail("context", exc, Context.empty(requested=requested, error=error_code(exc)))
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
        epoch = self._cache.epoch(scope)
        try:
            fetched = self._fetch_context(request, budget, self._cache.etag(key))
            self._cache.absorb(key, scope, fetched, deliver=False, epoch=epoch)
        except Exception as exc:
            self._cache.drop_on_auth_error(key, exc)
            logger.info("niadra: background context refresh failed (%s)", error_code(exc))
        finally:
            self._cache.end_refresh(key)

    def _send_batch(self, payloads: list[dict[str, Any]]) -> Any:
        return self._transport.request(self._core.batch_http(payloads))

    def _forget_scope(self, scope: str, *, ended: bool = True) -> None:
        """Drops a conversation's packs; when it `ended`, its voice line too, else only the line's reads
        (they were made for the pack being dropped)."""
        self._cache.purge_scope(scope)
        if ended:
            self._voice.drop(scope)
            return
        line = self._voice.find(scope)
        if line is not None:
            with self._voice.lock:
                line.reads = []
                line.speculating = None

    # The voice read path (niadra._voice).

    def _voice_pool(self) -> ThreadPoolExecutor:
        with self._voice.lock:
            if self._voice_workers is None:
                self._voice_workers = ThreadPoolExecutor(max_workers=32, thread_name_prefix="niadra-voice")
            return self._voice_workers

    def _voice_probe(self) -> None:
        """Measures the round trip to the region once per client, in the background."""
        if not self._voice.claim_probe():
            return
        with suppress(RuntimeError):  # the client is closing
            self._voice_pool().submit(self._probe)

    def _probe(self) -> None:
        samples = []
        for _ in range(2):  # the first may pay for the connection; the second is the round trip
            started = time.monotonic()
            try:
                self._transport.request(self._core.probe_http())
            except Exception as exc:
                logger.debug("niadra: round trip probe failed (%s)", error_code(exc))
                return
            samples.append(time.monotonic() - started)
        self._voice.rtt = self._core.probed(samples)

    def _start_budget(self, scope: str) -> float | None:
        """What is left of `context_voice_start` for the first read of a voice line; None without one."""
        line = self._voice.find(scope)
        if line is None or not line.reads:
            return None
        elapsed = time.monotonic() - line.reads[0].started
        return max(0.0, self._core.timeouts.context_voice_start - elapsed)

    def _voice_begin(self, request: ContextRequest) -> bool:
        key = cache_key(request)
        scope = self._core.scope_of(request.conversation_id, request.task_id)
        line = self._voice.line(scope)
        self._voice_probe()
        with self._voice.lock:
            line.request = request
            if self._cache.has(key) or line.in_flight():
                return True
            self._voice_read(line, key, request, None, self._core.timeouts.context_voice_start)
        return True

    def _voice_context(
        self, request: ContextRequest, query: str | None, budget: float, requested: Verification
    ) -> Context:
        """A voice turn: the pinned body from memory, and the slots of the read of its words when that
        read lands within `budget`. See `niadra._voice`."""
        started = time.monotonic()
        deadline = started + budget
        key = cache_key(request)
        scope = self._core.scope_of(request.conversation_id, request.task_id)
        line = self._voice.line(scope)
        self._voice_probe()
        background = self._core.timeouts.prefetch
        options = self._core.voice_options
        words = words_of(query)
        with self._voice.lock:
            line.request = request
            opening: list[TurnRead] = []
            if not self._cache.has(key):
                opening = line.in_flight() or [self._voice_read(line, key, request, query, background)]
        if opening:
            self._voice_wait(opening, deadline, key)
            if not self._cache.has(key):
                return self._voice_failed(next((r.failed for r in opening if r.failed), None), requested)
        chosen: TurnRead | None = None
        asked = bool(opening)
        if words:
            with self._voice.lock:
                candidates = line.covering(words, options.min_coverage)
                if not candidates:
                    candidates, asked = [self._voice_read(line, key, request, query, background)], True
            newest = candidates[0]
            if not newest.done:
                self._voice_wait([newest], deadline)
            chosen = next((read for read in candidates if read.ok), None)
            if chosen is None and newest.failed is not None and self._core.strict:
                raise newest.failed
        hit = self._cache.get(key, pinned=True)
        if hit is None:
            return self._voice_failed(None, requested)
        # A read this turn started revalidates the body; without one, an old body is revalidated now.
        if hit.freshness != "fresh" and not asked and not line.in_flight():
            self._refresh_later(key, scope, request, background)
        context = compose(hit.context, chosen)
        # A body this call waited for came over the network; otherwise it was already in memory.
        origin = "network" if opening else hit.context.origin
        return context.model_copy(
            update={"origin": origin, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
        )

    def _voice_failed(self, failure: BaseException | None, requested: Verification) -> Context:
        error = failure or APITimeoutError("POST /v1/context ran out of its time budget")
        return self._core.fail("context", error, Context.empty(requested=requested, error=error_code(error)))

    def _voice_wait(self, reads: Sequence[TurnRead], deadline: float, key: str | None = None) -> None:
        """Waits until one of `reads` is done (with `key`, until its pack is here) or `deadline`."""
        while True:
            pending = [read.handle for read in reads if not read.done and read.handle is not None]
            remaining = deadline - time.monotonic()
            if not pending or remaining <= 0 or (key is not None and self._cache.has(key)):
                return
            wait_for(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if key is None:
                return

    def _voice_read(
        self,
        line: VoiceLine,
        key: str,
        request: ContextRequest,
        query: str | None,
        budget: float,
        *,
        speculative: bool = False,
    ) -> TurnRead:
        """Starts one read of a line in the background. Called with the lines' lock held."""
        read = TurnRead(words_of(query), speculative=speculative)
        line.add(read)
        body = self._core.voice_body(request, query)
        # A `not_modified` answer carries no `pack`, so a read as data asks for the whole answer.
        known = self._cache.etag(key) if request.format != "json" else None
        epoch = self._cache.epoch(line.scope)
        try:
            read.handle = self._voice_pool().submit(
                self._voice_run, line, read, key, body, budget, known, epoch
            )
        except RuntimeError as exc:  # the client is closing
            read.failed, read.done = exc, True
        return read

    def _voice_run(
        self,
        line: VoiceLine,
        read: TurnRead,
        key: str,
        body: ContextRequest,
        budget: float,
        known: str | None,
        epoch: int,
    ) -> None:
        following: str | None = None
        try:
            fetched = self._fetch_context(body, budget, known)
            self._core.store_read(self._cache, key, line.scope, fetched, epoch)
            read.result = fetched
        except Exception as exc:
            self._cache.drop_on_auth_error(key, exc)
            read.failed = exc
            logger.info("niadra: voice read failed (%s)", error_code(exc))
        finally:
            with self._voice.lock:
                read.done = True
                if line.speculating is read:
                    line.speculating = None
                    following, line.waiting = line.waiting, None
        if following is not None:
            self._voice_speculate(line, following)

    def _voice_heard(self, scope: str, text: str | None) -> None:
        """A partial transcript of a voice line: read the turn with it once it has settled."""
        line = self._voice.find(scope)
        if line is None or text is None:
            return
        with self._voice.lock:
            if line.closed or line.request is None:
                return
            if text != line.heard:
                line.heard, line.heard_at = text, time.monotonic()
            if line.settling:
                return
            line.settling = True
        try:
            self._voice_pool().submit(self._voice_settle, line)
        except RuntimeError:
            line.settling = False

    def _voice_settle(self, line: VoiceLine) -> None:
        settle = self._core.voice_options.settle
        while True:
            with self._voice.lock:
                if line.closed or line.heard is None:
                    line.settling = False
                    return
                left = line.heard_at + settle - time.monotonic()
                if left <= 0:
                    text, line.heard, line.settling = line.heard, None, False
                    break
            time.sleep(left)
        self._voice_speculate(line, text)

    def _voice_speculate(self, line: VoiceLine, text: str) -> None:
        """Reads the turn with a settled partial: one such read in flight per line, the newest text next."""
        words = words_of(text)
        with self._voice.lock:
            request = line.request
            if line.closed or request is None or not words or line.already_read(words):
                return
            if line.speculating is not None and not line.speculating.done:
                line.waiting = text
                return
            line.speculating = self._voice_read(
                line, cache_key(request), request, text, self._core.timeouts.prefetch, speculative=True
            )

    def _turn_sender(self) -> SyncTurnSender:
        assert isinstance(self.turns.sender, SyncTurnSender)
        return self.turns.sender

    def _send_now(
        self, method: str, item: BaseModel, conversation_id: str | None, task_id: str | None
    ) -> BatchResponse | None:
        scope = self._core.scope_of(conversation_id, task_id)
        if scope:
            self._forget_scope(scope, ended=False)
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
