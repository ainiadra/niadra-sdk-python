"""Writes the agent's code made that must reach Niadra, sent in the background and in order: coordination
declarations, and agent state writes made while Niadra was out of reach.

A write carries its `Idempotency-Key`, so sending it again is harmless. A failure that may pass (a
connection, a 5xx, a 429, a 421) keeps the write at the front and pauses the sender, doubling the pause while
the API stays down; any other failure hands the error to the write's `settled` callback, which is also how a
write that went through hears the answer. Putting a write never waits and never does I/O. Past `capacity`
writes the oldest go, counted and logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from niadra._queue import _Pacing, is_retryable
from niadra._transport import Request

logger = logging.getLogger("niadra")

CAPACITY = 1000


@dataclass
class Write:
    request: Request
    settled: Callable[[Any, Exception | None], None] | None = None
    """Called with the answer, or with the error that will not pass."""


class Outbox:
    """The pending writes; what the two senders share."""

    def __init__(self, interval: float = 1.0, capacity: int = CAPACITY) -> None:
        self._writes: deque[Write] = deque()
        self._lock = threading.Lock()
        self._capacity = capacity
        self._pacing = _Pacing(interval)
        self.dropped = 0

    def put(self, write: Write) -> None:
        with self._lock:
            self._writes.append(write)
            while len(self._writes) > self._capacity:
                self._writes.popleft()
                self.dropped += 1
                logger.warning("niadra: the write outbox is full; the oldest write was dropped")

    def __len__(self) -> int:
        with self._lock:
            return len(self._writes)

    def _next(self) -> Write | None:
        with self._lock:
            return self._writes[0] if self._writes else None

    def _settle(self, write: Write, answer: Any, error: Exception | None) -> bool:
        """Applies an attempt's outcome. False when the write stays for later."""
        if error is not None and is_retryable(error):
            self._pacing.failed()
            return False
        with self._lock:
            if self._writes and self._writes[0] is write:
                self._writes.popleft()
        self._pacing.succeeded()
        if error is not None:
            logger.warning("niadra: a write was refused (%s)", getattr(error, "code", type(error).__name__))
        if write.settled is not None:
            try:
                write.settled(answer, error)
            except Exception:
                logger.warning("niadra: a write's callback failed", exc_info=True)
        return True

    def pause(self) -> float:
        return self._pacing.remaining()


class SyncOutbox(Outbox):
    """A daemon thread, started by the first write."""

    def __init__(self, send: Callable[[Request], Any], interval: float = 1.0) -> None:
        super().__init__(interval)
        self._send = send
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = threading.Lock()
        self._busy = threading.Lock()
        self._stopping = False

    def put(self, write: Write) -> None:
        super().put(write)
        if self._thread is None:
            with self._start:
                if self._thread is None and not self._stopping:
                    self._thread = threading.Thread(target=self._run, name="niadra-writes", daemon=True)
                    self._thread.start()
        self._wake.set()

    def flush(self, timeout: float | None = None) -> bool:
        """Sends what waits, from the calling thread. True when nothing is left."""
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._busy.acquire(timeout=-1 if deadline is None else max(0.0, deadline - time.monotonic())):
            return False
        try:
            while (write := self._next()) is not None:
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                if not self._attempt(write):
                    return False
            return True
        finally:
            self._busy.release()

    def stop(self, timeout: float | None = None) -> bool:
        self._stopping = True
        self._wake.set()
        return self.flush(timeout)

    def _run(self) -> None:
        while not self._stopping:
            self._wake.wait(timeout=self.pause() or None)
            self._wake.clear()
            if self._stopping:
                return
            if self.pause():
                continue
            with self._busy:
                while (write := self._next()) is not None and self._attempt(write):
                    pass

    def _attempt(self, write: Write) -> bool:
        try:
            answer = self._send(write.request)
        except Exception as error:
            return self._settle(write, None, error)
        return self._settle(write, answer, None)


class AsyncOutbox(Outbox):
    """A task on the running loop. Outside a running loop, writes wait for `flush()`."""

    def __init__(self, send: Callable[[Request], Awaitable[Any]], interval: float = 1.0) -> None:
        super().__init__(interval)
        self._send = send
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._busy: asyncio.Lock | None = None
        self._stopping = False

    def put(self, write: Write) -> None:
        super().put(write)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._stopping:
            return
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._wake, self._busy = asyncio.Event(), asyncio.Lock()
            self._task = loop.create_task(self._run(), name="niadra-writes")
        assert self._wake is not None
        self._wake.set()

    async def flush(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        busy = self._busy_lock()
        try:
            await asyncio.wait_for(
                busy.acquire(), None if deadline is None else max(0.0, deadline - time.monotonic())
            )
        except asyncio.TimeoutError:
            return False
        try:
            while (write := self._next()) is not None:
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                if not await self._attempt(write):
                    return False
            return True
        finally:
            busy.release()

    async def stop(self, timeout: float | None = None) -> bool:
        self._stopping = True
        flushed = await self.flush(timeout)
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            if task.get_loop() is asyncio.get_running_loop():
                await asyncio.gather(task, return_exceptions=True)
        return flushed

    def _busy_lock(self) -> asyncio.Lock:
        if self._busy is None:
            self._busy = asyncio.Lock()
        return self._busy

    async def _run(self) -> None:
        wake = self._wake
        assert wake is not None
        while not self._stopping:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=self.pause() or None)
            wake.clear()
            if self._stopping or self.pause():
                continue
            async with self._busy_lock():
                while (write := self._next()) is not None and await self._attempt(write):
                    pass

    async def _attempt(self, write: Write) -> bool:
        try:
            answer = await self._send(write.request)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._settle(write, None, error)
        return self._settle(write, answer, None)
