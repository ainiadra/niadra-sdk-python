"""The local queue behind `track()` and its two flushers, a thread and an asyncio task.

Items are serialized when they are queued, so a value that cannot become JSON is dropped
right away, with a log line, instead of poisoning a batch later. A batch that fails with a
retryable error after the transport's attempts goes back to the front of the queue and the
flusher pauses; a batch rejected with any other 4xx is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from niadra.errors import APIConnectionError, NiadraError, RateLimitError, ServerError, WrongCellError
from niadra.models.events import MAX_BATCH_ITEMS, BatchResponse, HeartbeatItem
from niadra.options import QueueOptions

logger = logging.getLogger("niadra")

Payload = dict[str, Any]
MAX_PAUSE = 30.0


def serialize(item: BaseModel) -> Payload | None:
    """The JSON form of a batch item, or None when some field cannot be encoded."""
    try:
        payload = item.model_dump(mode="json", exclude_none=True)
        json.dumps(payload)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "niadra: dropped a %s that cannot be serialized (%s)", _item_type(item), type(exc).__name__
        )
        return None
    return payload


def _item_type(item: BaseModel) -> str:
    return str(getattr(item, "type", type(item).__name__))


def is_retryable(error: Exception) -> bool:
    return isinstance(error, (APIConnectionError, ServerError, RateLimitError, WrongCellError))


def is_turn(payload: Payload) -> bool:
    """A message of a conversation: what the other agents read in `live` while it goes on."""
    return (
        payload.get("type", "event") == "event"
        and payload.get("kind", "message") == "message"
        and bool(payload.get("conversation_id"))
    )


class EventBuffer:
    """A bounded, thread-safe FIFO of serialized batch items.

    It is due `interval` seconds after its oldest item arrived, `turn_interval` seconds after
    its oldest conversation turn arrived, or at once with `batch_size` items waiting.
    """

    def __init__(self, options: QueueOptions) -> None:
        self._options = options
        self._items: deque[Payload] = deque()
        self._lock = threading.Lock()
        self._first_at: float | None = None
        self._first_turn_at: float | None = None
        self.dropped = 0

    def put(self, payload: Payload) -> bool:
        with self._lock:
            if len(self._items) >= self._options.capacity:
                self.dropped += 1
                if self.dropped == 1 or self.dropped % 1000 == 0:
                    logger.warning("niadra: event queue full, %d events dropped so far", self.dropped)
                return False
            now = time.monotonic()
            if not self._items:
                self._first_at = now
            if self._first_turn_at is None and is_turn(payload):
                self._first_turn_at = now
            self._items.append(payload)
            return True

    def requeue(self, payloads: list[Payload]) -> None:
        """Puts a failed batch back at the front, keeping only what still fits."""
        with self._lock:
            room = self._options.capacity - len(self._items)
            keep = payloads[: max(room, 0)]
            self.dropped += len(payloads) - len(keep)
            self._items.extendleft(reversed(keep))
            now = time.monotonic()
            if self._items and self._first_at is None:
                self._first_at = now
            if self._first_turn_at is None and any(is_turn(payload) for payload in keep):
                self._first_turn_at = now

    def take(self, limit: int = MAX_BATCH_ITEMS) -> list[Payload]:
        with self._lock:
            count = min(limit, len(self._items))
            batch = [self._items.popleft() for _ in range(count)]
            now = time.monotonic()
            self._first_at = now if self._items else None
            self._first_turn_at = now if any(is_turn(payload) for payload in self._items) else None
            return batch

    def next_due(self) -> float | None:
        """The monotonic time at which what is waiting should leave, or None when nothing is."""
        with self._lock:
            return self._next_due()

    def _next_due(self) -> float | None:
        if not self._items or self._first_at is None:
            return None
        if len(self._items) >= self._options.batch_size:
            return self._first_at
        at = self._first_at + self._options.interval
        if self._first_turn_at is not None:
            at = min(at, self._first_turn_at + self._options.turn_interval)
        return at

    def due(self) -> bool:
        at = self.next_due()
        return at is not None and time.monotonic() >= at

    def wait_hint(self) -> float:
        """Seconds until what is waiting is due, for the flusher to sleep on."""
        at = self.next_due()
        if at is None:
            return self._options.interval
        return max(0.0, at - time.monotonic())

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class Heartbeat:
    """Counts items handed to the API and emits one `heartbeat` item per window.

    The server compares these counters with what it received to report per-source coverage,
    which is how a pack can say that a source went silent.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._window_start = datetime.now(timezone.utc)
        self._opened = time.monotonic()
        self._sent = 0
        self._lock = threading.Lock()

    def count(self, n: int) -> None:
        with self._lock:
            self._sent += n

    def due_item(self) -> Payload | None:
        with self._lock:
            if time.monotonic() - self._opened < self._interval or self._sent == 0:
                return None
            item = HeartbeatItem(window_start=self._window_start, sent=self._sent)
            self._window_start = datetime.now(timezone.utc)
            self._opened = time.monotonic()
            self._sent = 0
        return item.model_dump(mode="json")


def _report(response: Any, size: int) -> None:
    try:
        result = BatchResponse.model_validate(response)
    except ValueError:
        return
    if result.errors:
        codes = sorted({error.code for error in result.errors})
        logger.warning("niadra: %d of %d events rejected (%s)", len(result.errors), size, ", ".join(codes))


class _Pacing:
    """The pause after a failing flush, doubling up to `MAX_PAUSE` while the API stays down."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._pause = 0.0
        self._resume_at = 0.0

    def failed(self) -> None:
        self._pause = min(MAX_PAUSE, max(self._interval, self._pause * 2))
        self._resume_at = time.monotonic() + self._pause

    def succeeded(self) -> None:
        self._pause = 0.0
        self._resume_at = 0.0

    def remaining(self) -> float:
        return max(0.0, self._resume_at - time.monotonic())


def _prepare(buffer: EventBuffer, heartbeat: Heartbeat) -> list[Payload]:
    batch = buffer.take()
    beat = heartbeat.due_item()
    if beat is not None:
        if len(batch) < MAX_BATCH_ITEMS:
            batch.append(beat)
        else:
            buffer.requeue([beat])
    return batch


def _settle(buffer: EventBuffer, batch: list[Payload], error: Exception, pacing: _Pacing) -> None:
    if is_retryable(error):
        buffer.requeue(batch)
        pacing.failed()
        logger.warning(
            "niadra: could not deliver %d events, will retry (%s)", len(batch), type(error).__name__
        )
    else:
        buffer.dropped += len(batch)
        code = getattr(error, "code", type(error).__name__)
        logger.warning("niadra: dropped %d events rejected by the API (%s)", len(batch), code)


class SyncFlusher:
    def __init__(
        self,
        buffer: EventBuffer,
        send: Callable[[list[Payload]], Any],
        options: QueueOptions,
    ) -> None:
        self._buffer = buffer
        self._send = send
        self._heartbeat = Heartbeat(options.heartbeat_interval)
        self._pacing = _Pacing(options.interval)
        self._wake = threading.Condition()
        self._sleep_until = math.inf
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def notify(self) -> None:
        """Wakes the sender when what is waiting became due before the time it sleeps until."""
        self._ensure_started()
        at = self._buffer.next_due()
        if at is None:
            return
        with self._wake:
            if at < self._sleep_until:
                self._wake.notify()

    def flush(self, timeout: float | None = None) -> bool:
        """Sends everything queued now, from the calling thread. True when the queue emptied."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self._buffer):
            if deadline is not None and time.monotonic() >= deadline:
                return False
            if not self._send_one():
                return False
        return True

    def stop(self, timeout: float | None = None) -> bool:
        with self._wake:
            self._stopping = True
            self._wake.notify()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        return self.flush(timeout)

    def _ensure_started(self) -> None:
        if self._thread is not None:
            return
        with self._start_lock:
            if self._thread is None and not self._stopping:
                self._thread = threading.Thread(target=self._run, name="niadra-flush", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            with self._wake:
                if self._stopping:
                    return
                wait = max(self._pacing.remaining(), self._buffer.wait_hint(), 0.01)
                self._sleep_until = time.monotonic() + wait
                self._wake.wait(timeout=wait)
                if self._stopping:
                    return
            if self._buffer.due() and not self._pacing.remaining():
                self._send_one()

    def _send_one(self) -> bool:
        batch = _prepare(self._buffer, self._heartbeat)
        if not batch:
            return True
        try:
            response = self._send(batch)
        except NiadraError as error:
            _settle(self._buffer, batch, error, self._pacing)
            return not is_retryable(error)
        except Exception:
            logger.exception("niadra: unexpected error while sending events")
            self._buffer.requeue(batch)
            self._pacing.failed()
            return False
        self._pacing.succeeded()
        self._heartbeat.count(len(batch))
        _report(response, len(batch))
        return True


class AsyncFlusher:
    def __init__(
        self,
        buffer: EventBuffer,
        send: Callable[[list[Payload]], Awaitable[Any]],
        options: QueueOptions,
    ) -> None:
        self._buffer = buffer
        self._send = send
        self._heartbeat = Heartbeat(options.heartbeat_interval)
        self._pacing = _Pacing(options.interval)
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._sleep_until = math.inf
        self._stopping = False

    def notify(self) -> None:
        """Starts the flush task on the running loop, if there is one, and wakes it when what is
        waiting became due before the time it sleeps until.

        Outside a running loop items simply wait for the next `flush()`.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._stopping:
            return
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._wake = asyncio.Event()
            self._sleep_until = math.inf
            self._task = loop.create_task(self._run(), name="niadra-flush")
        at = self._buffer.next_due()
        if at is not None and at < self._sleep_until and self._wake is not None:
            self._wake.set()

    async def flush(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self._buffer):
            if deadline is not None and time.monotonic() >= deadline:
                return False
            if not await self._send_one():
                return False
        return True

    async def stop(self, timeout: float | None = None) -> bool:
        self._stopping = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return await self.flush(timeout)

    async def _run(self) -> None:
        wake = self._wake
        assert wake is not None
        while not self._stopping:
            wait = max(self._pacing.remaining(), self._buffer.wait_hint(), 0.01)
            self._sleep_until = time.monotonic() + wait
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=wait)
            wake.clear()
            if self._buffer.due() and not self._pacing.remaining():
                await self._send_one()

    async def _send_one(self) -> bool:
        batch = _prepare(self._buffer, self._heartbeat)
        if not batch:
            return True
        try:
            response = await self._send(batch)
        except NiadraError as error:
            _settle(self._buffer, batch, error, self._pacing)
            return not is_retryable(error)
        except asyncio.CancelledError:
            self._buffer.requeue(batch)
            raise
        except Exception:
            logger.exception("niadra: unexpected error while sending events")
            self._buffer.requeue(batch)
            self._pacing.failed()
            return False
        self._pacing.succeeded()
        self._heartbeat.count(len(batch))
        _report(response, len(batch))
        return True
