"""The sender of the turn queue: `POST /v1/turns`, in the background, one request in flight per client.

A thread for `Niadra`, a task for `AsyncNiadra`, which builds its records on a worker thread so the event
loop never computes a digest. Each request carries up to 50 records in gzip, at most 4 MB compressed and
16 MB decompressed; a record too large alone leaves with digests only, so a `turn_too_large` never loops.

What the answer decides:

- 200 or 207: the turns are taken. A turn refused with `content_mode_refused` (the source records less
  than the SDK sent) goes again with digests only, which every source accepts; any other refusal is counted
  and dropped.
- 404: the space does not record turns. The batch is dropped and the recorder stops recording for a while.
- 421, 429, 5xx and network errors: the batch goes back to the front of the queue and the sender pauses,
  doubling the pause while the API stays down.
- Any other error: the batch is counted and dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from niadra._queue import _Pacing, is_retryable
from niadra._transport import Request
from niadra.errors import APIError, NiadraError, NotFoundError, explain
from niadra.models.turns import TurnsResponse
from niadra.turns.capture import TurnFrame
from niadra.turns.queue import TurnQueue
from niadra.turns.record import as_hash_only, build

if TYPE_CHECKING:
    from niadra.turns.recorder import TurnRecorder

logger = logging.getLogger("niadra")

MAX_TURNS = 50
MAX_COMPRESSED = 4 * 1024 * 1024
MAX_DECODED = 16 * 1024 * 1024
# Headroom under the server's limits for the envelope and the encoder.
_COMPRESSED_ROOM = MAX_COMPRESSED - 64 * 1024
_DECODED_ROOM = MAX_DECODED - 256 * 1024
_HEADERS = {"Content-Type": "application/json", "Content-Encoding": "gzip"}


@dataclass
class Batch:
    frames: list[TurnFrame]
    records: list[dict[str, Any]]
    body: bytes


def _encode(records: list[dict[str, Any]]) -> bytes:
    raw = json.dumps({"turns": records}, ensure_ascii=False, separators=(",", ":")).encode()
    return gzip.compress(raw, compresslevel=6, mtime=0)


def _sized(record: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """The record as it may travel, and its JSON's bytes: one past the limits alone keeps digests only."""
    size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode())
    if size > _DECODED_ROOM:
        record = as_hash_only(record)
        size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode())
    return record, size


def batches(frames: list[TurnFrame], records: list[dict[str, Any]]) -> list[Batch]:
    """Requests of up to 50 records within the size limits, in queue order."""
    out: list[Batch] = []
    group: list[tuple[TurnFrame, dict[str, Any]]] = []
    group_bytes = 0

    def close() -> None:
        if group:
            out.extend(_compressed([f for f, _ in group], [r for _, r in group]))

    for frame, raw in zip(frames, records, strict=True):
        record, size = _sized(raw)
        if group and (len(group) == MAX_TURNS or group_bytes + size > _DECODED_ROOM):
            close()
            group, group_bytes = [], 0
        group.append((frame, record))
        group_bytes += size
    close()
    return out


def _compressed(frames: list[TurnFrame], records: list[dict[str, Any]]) -> list[Batch]:
    body = _encode(records)
    if len(body) <= _COMPRESSED_ROOM:
        return [Batch(frames, records, body)]
    if len(records) == 1:
        record = as_hash_only(records[0])
        return [Batch(frames, [record], _encode([record]))]
    half = len(records) // 2
    return _compressed(frames[:half], records[:half]) + _compressed(frames[half:], records[half:])


class _Sender:
    """What both senders share: building the requests and reading the answers. No I/O of its own."""

    def __init__(self, recorder: TurnRecorder, queue: TurnQueue, interval: float) -> None:
        self._recorder = recorder
        self._queue = queue
        self._pacing = _Pacing(interval)

    def prepare(self, frames: list[TurnFrame]) -> list[Batch]:
        """The requests for `frames`: records built in their content mode, values written to the company's
        store first in `pointer` mode. CPU work, and the store's I/O: never on the agent's path."""
        records = [
            build(f, self._recorder.mode_of(f), store=self._recorder.blob_store, claims=self._recorder.claims)
            for f in frames
        ]
        return batches(frames, records)

    def request(self, batch: Batch, timeout: float) -> Request:
        return Request("POST", "/v1/turns", content=batch.body, headers=_HEADERS, timeout=timeout)

    def settle(self, batch: Batch, answer: Any = None, error: Exception | None = None) -> bool:
        """Applies the answer to a batch. True when the sender may go on, False when it should pause."""
        if error is None:
            self._pacing.succeeded()
            self._accepted(batch, answer)
            return True
        if isinstance(error, NotFoundError):
            self._recorder.not_recorded(len(batch.frames), off=True)
            return True
        if isinstance(error, NiadraError) and is_retryable(error):
            self._queue.requeue(batch.frames)
            self._pacing.failed()
            logger.warning(
                "niadra: could not deliver %d turns, will retry (%s)", len(batch.frames), type(error).__name__
            )
            return False
        if isinstance(error, APIError) and error.status_code == 413 and len(batch.frames) == 1:
            frame = batch.frames[0]
            if frame.mode != "hash_only":
                frame.mode = "hash_only"
                self._queue.requeue(batch.frames)
                return True
        self._recorder.rejected(len(batch.frames), {explain(error)})
        return True

    def _accepted(self, batch: Batch, answer: Any) -> None:
        try:
            result = TurnsResponse.model_validate(answer)
        except ValueError:
            return
        self._recorder.sent(result.accepted, result.duplicates)
        again: list[TurnFrame] = []
        reasons: set[str] = set()
        refused = 0
        for error in result.errors:
            if not 0 <= error.index < len(batch.frames):
                continue
            frame = batch.frames[error.index]
            if error.code == "content_mode_refused" and frame.mode != "hash_only":
                frame.mode = "hash_only"
                self._recorder.mode_refused()
                again.append(frame)
            else:
                refused += 1
                reasons.add(f"{error.code}: {error.detail[:300]}" if error.detail else error.code)
        if again:
            self._queue.requeue(again)
        if refused:
            self._recorder.rejected(refused, reasons)

    def wait_hint(self) -> float:
        """Seconds until the next batch is due: infinite with nothing waiting (a new turn wakes the sender),
        and at least 10 ms while paused, so a sender never spins."""
        at = self._queue.next_due()
        if at is None:
            return math.inf
        pause = self._pacing.remaining()
        hint = max(0.0, at - time.monotonic())
        return 0.0 if pause == 0 and hint == 0 else max(pause, hint, 0.01)

    def due(self) -> bool:
        at = self._queue.next_due()
        return at is not None and time.monotonic() >= at and not self._pacing.remaining()


class SyncTurnSender(_Sender):
    """A daemon thread, started by the first closed turn."""

    def __init__(
        self,
        recorder: TurnRecorder,
        queue: TurnQueue,
        interval: float,
        send: Callable[[Request], Any],
        timeout: float,
        refresh: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(recorder, queue, interval)
        self._send = send
        self._timeout = timeout
        self._refresh = refresh
        self._wake = threading.Condition()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._send_lock = threading.Lock()

    def notify(self) -> None:
        if self._thread is None:
            with self._start_lock:
                if self._thread is None and not self._stopping:
                    self._thread = threading.Thread(target=self._run, name="niadra-turns", daemon=True)
                    self._thread.start()
        with self._wake:
            self._wake.notify()

    def flush(self, timeout: float | None = None) -> bool:
        """Sends everything queued, from the calling thread. True when the queue emptied."""
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._send_lock.acquire(
            timeout=-1 if deadline is None else max(0.0, deadline - time.monotonic())
        ):
            return False
        try:
            while len(self._queue):
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                if not self._send_next():
                    return False
            return True
        finally:
            self._send_lock.release()

    def stop(self, timeout: float | None = None) -> bool:
        with self._wake:
            self._stopping = True
            self._wake.notify()
        flushed = self.flush(timeout)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        return flushed

    def _run(self) -> None:
        while True:
            with self._wake:
                if self._stopping:
                    return
                wait = self.wait_hint()
                if wait > 0:
                    self._wake.wait(timeout=None if math.isinf(wait) else wait)
                if self._stopping:
                    return
            with self._send_lock:
                if self.due():
                    self._send_next()

    def _send_next(self) -> bool:
        frames = self._queue.take(MAX_TURNS)
        if not frames:
            return True
        if self._refresh is not None:
            self._refresh()
        if not self._recorder.recording:
            self._recorder.not_recorded(len(frames), off=False)
            return True
        try:
            prepared = self.prepare(frames)
        except Exception:
            logger.exception("niadra: turn records could not be built")
            self._recorder.rejected(len(frames), {"build_failed"})
            return True
        for i, batch in enumerate(prepared):
            try:
                answer = self._send(self.request(batch, self._timeout))
            except Exception as error:
                if not self.settle(batch, error=error):
                    self._queue.requeue([f for b in prepared[i + 1 :] for f in b.frames])
                    return False
                continue
            self.settle(batch, answer)
        return True


class AsyncTurnSender(_Sender):
    """A task on the running loop. Outside a running loop, closed turns wait for `flush()`."""

    def __init__(
        self,
        recorder: TurnRecorder,
        queue: TurnQueue,
        interval: float,
        send: Callable[[Request], Awaitable[Any]],
        timeout: float,
        refresh: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        super().__init__(recorder, queue, interval)
        self._send = send
        self._timeout = timeout
        self._refresh = refresh
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self._busy: asyncio.Lock | None = None
        self._busy_loop: asyncio.AbstractEventLoop | None = None

    def notify(self) -> None:
        """Called from the agent's path, maybe from another thread: never raises, never waits."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._stopping:
            return
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._wake = asyncio.Event()
            self._task = loop.create_task(self._run(), name="niadra-turns")
        if self._wake is not None:
            self._wake.set()

    async def flush(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        lock = self._lock()
        try:
            await asyncio.wait_for(
                lock.acquire(), None if deadline is None else max(0.0, deadline - time.monotonic())
            )
        except asyncio.TimeoutError:
            return False
        try:
            while len(self._queue):
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                if not await self._send_next():
                    return False
            return True
        finally:
            lock.release()

    async def stop(self, timeout: float | None = None) -> bool:
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
        flushed = await self.flush(timeout)
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            if task.get_loop() is asyncio.get_running_loop():
                await asyncio.gather(task, return_exceptions=True)
        return flushed

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._busy is None or self._busy_loop is not loop:
            self._busy, self._busy_loop = asyncio.Lock(), loop
        return self._busy

    async def _run(self) -> None:
        wake = self._wake
        assert wake is not None
        while not self._stopping:
            wait = self.wait_hint()
            if wait > 0:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=None if math.isinf(wait) else wait)
            else:
                await asyncio.sleep(0)  # what is due leaves now, after other tasks had their turn
            wake.clear()
            if self._stopping:
                return
            async with self._lock():
                if self.due():
                    await self._send_next()

    async def _send_next(self) -> bool:
        frames = self._queue.take(MAX_TURNS)
        if not frames:
            return True
        if self._refresh is not None:
            await self._refresh()
        if not self._recorder.recording:
            self._recorder.not_recorded(len(frames), off=False)
            return True
        try:
            prepared = await asyncio.to_thread(self.prepare, frames)
        except asyncio.CancelledError:
            self._queue.requeue(frames)
            raise
        except Exception:
            logger.exception("niadra: turn records could not be built")
            self._recorder.rejected(len(frames), {"build_failed"})
            return True
        for i, batch in enumerate(prepared):
            try:
                answer = await self._send(self.request(batch, self._timeout))
            except asyncio.CancelledError:
                self._queue.requeue([f for b in prepared[i:] for f in b.frames])
                raise
            except Exception as error:
                if not self.settle(batch, error=error):
                    self._queue.requeue([f for b in prepared[i + 1 :] for f in b.frames])
                    return False
                continue
            self.settle(batch, answer)
        return True
