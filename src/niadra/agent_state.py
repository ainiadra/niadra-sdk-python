"""The agent's working state: a small state its code keeps between turns, with a declared schema
(`spec/agent-state.md`). Written by code, never by a model.

```python
state = conversation.agent_state.get()
conversation.agent_state.put({"offer": {"status": "accepted"}}, mode="merge_by_key", if_version=state.version)
```

`cas` replaces the whole state at `if_version` (0 when it must not exist yet); `merge_by_key` replaces the
top-level fields it names and removes a field written as `{"$delete": true}`, so sub-agents writing
different fields never lose each other's writes.

The SDK keeps, per scope and agent, the last version it wrote or read, and serves the higher of that and what
a read brings (read your writes). With Niadra out of reach, a read serves that copy (`degraded`), and a write
applies to it at once and leaves again later with the same `if_version`: the answer says `pending`. A
compare-and-swap that then conflicts is never merged in silence: it lands in `conflicts` and is logged. Over
the cap a write is not stored and the previous state stays (`reason: over_cap`), never an error.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from niadra._base import error_code
from niadra._outbox import Outbox, Write
from niadra._queue import is_retryable
from niadra._transport import Request
from niadra.errors import APIError
from niadra.models.common import Handle
from niadra.models.state import AgentState, AgentStateScope, AgentStateWrite, AgentStateWriteResult
from niadra.turns.capture import current_turn

logger = logging.getLogger("niadra")

Mode = Literal["cas", "merge_by_key"]
DELETE = {"$delete": True}
"""A field written as this, in `merge_by_key`, is removed."""


@dataclass(frozen=True)
class WorkingState:
    body: dict[str, Any]
    version: int
    updated_at: datetime | None = None
    degraded: bool = False
    """Served from the SDK's copy because Niadra did not answer."""


@dataclass(frozen=True)
class StateWrite:
    """A write's answer: `stored` and the version after it; `reason` is `over_cap` or `not_declared_field`
    (not stored, the state as it was), or `conflict` (412: the version moved; read and try again). `pending`
    says Niadra did not answer and the write leaves later, already applied to the SDK's copy."""

    stored: bool
    version: int
    reason: str | None = None
    pending: bool = False


@dataclass(frozen=True)
class Conflict:
    """A write sent again after an outage that met a newer version."""

    scope: dict[str, str]
    agent: str
    body: dict[str, Any]
    if_version: int | None


@dataclass
class _Held:
    body: dict[str, Any]
    version: int
    updated_at: datetime | None = None
    pending: int = 0


class AgentStates:
    """The client's copies, by scope and agent, and the writes waiting for Niadra."""

    def __init__(self, outbox: Outbox) -> None:
        self.outbox = outbox
        self._held: dict[tuple[str, str, str], _Held] = {}
        self._lock = threading.Lock()
        self.conflicts: list[Conflict] = []

    @staticmethod
    def read_http(scope: Mapping[str, str], agent: str, budget: float) -> Request:
        body = {"scope": dict(scope), "agent": agent}
        return Request(
            "POST", "/v1/agent-state/read", json=body, timeout=budget, budget=budget, max_attempts=1
        )

    @staticmethod
    def write_http(write: AgentStateWrite, budget: float | None = None) -> Request:
        body = write.model_dump(mode="json", exclude_none=True)
        if budget is None:
            return Request("PUT", "/v1/agent-state", json=body)
        return Request("PUT", "/v1/agent-state", json=body, timeout=budget, budget=budget, max_attempts=1)

    def write(
        self,
        scope: Mapping[str, str],
        agent: str,
        body: Mapping[str, Any],
        *,
        mode: Mode,
        if_version: int | None,
        subject: Handle | None,
    ) -> AgentStateWrite:
        return AgentStateWrite(
            scope=AgentStateScope.model_validate(dict(scope)),
            agent=agent,
            body=dict(body),
            mode=mode,
            if_version=if_version,
            subject=subject,
        )

    def read(self, scope: Mapping[str, str], agent: str, data: Any) -> WorkingState:
        state = AgentState.model_validate(data)
        key = _key(scope, agent)
        with self._lock:
            held = self._held.get(key)
            if held is None or (state.version >= held.version and not held.pending):
                held = _Held(dict(state.body), state.version, state.updated_at)
                self._held[key] = held
            served = WorkingState(copy.deepcopy(held.body), held.version, held.updated_at)
        _read(served.version)
        return served

    def unread(self, scope: Mapping[str, str], agent: str) -> WorkingState:
        """What a read serves when Niadra did not answer: the copy, or an empty state at version 0."""
        with self._lock:
            held = self._held.get(_key(scope, agent))
            served = (
                WorkingState(copy.deepcopy(held.body), held.version, held.updated_at, degraded=True)
                if held is not None
                else WorkingState({}, 0, degraded=True)
            )
        _read(served.version)
        return served

    def written(self, write: AgentStateWrite, data: Any) -> StateWrite:
        result = AgentStateWriteResult.model_validate(data)
        if result.stored:
            with self._lock:
                key = _key(write.scope.model_dump(mode="json"), write.agent)
                held = self._held.get(key)
                base = held.body if held is not None and write.mode == "merge_by_key" else {}
                if held is None or result.version >= held.version:
                    self._held[key] = _Held(_apply(base, write), result.version, pending=0)
        return StateWrite(result.stored, result.version, result.reason)

    def refused(self, write: AgentStateWrite, error: Exception) -> StateWrite | None:
        """A write Niadra answered with an error: a conflict is the code's to handle; None for any other."""
        if isinstance(error, APIError) and (error.status_code == 412 or error.code == "agent_state_conflict"):
            with self._lock:
                held = self._held.get(_key(write.scope.model_dump(mode="json"), write.agent))
            return StateWrite(False, held.version if held is not None else 0, "conflict")
        return None

    def later(self, write: AgentStateWrite) -> StateWrite:
        """Niadra did not answer: the write applies to the copy now and leaves again later."""
        key = _key(write.scope.model_dump(mode="json"), write.agent)
        with self._lock:
            held = self._held.get(key) or _Held({}, 0)
            if write.mode == "cas" and write.if_version is not None and write.if_version != held.version:
                return StateWrite(False, held.version, "conflict")
            held = _Held(_apply(held.body if write.mode == "merge_by_key" else {}, write), held.version + 1)
            held.pending = self._held[key].pending + 1 if key in self._held else 1
            self._held[key] = held
            version = held.version
        self.outbox.put(
            Write(self.write_http(write), lambda answer, error: self._resent(write, answer, error))
        )
        return StateWrite(True, version, pending=True)

    def _resent(self, write: AgentStateWrite, answer: Any, error: Exception | None) -> None:
        key = _key(write.scope.model_dump(mode="json"), write.agent)
        if error is None:
            result = AgentStateWriteResult.model_validate(answer)
            with self._lock:
                held = self._held.get(key)
                if held is not None:
                    held.pending = max(0, held.pending - 1)
                    held.version = max(held.version, result.version)
            return
        with self._lock:
            self._held.pop(key, None)  # the copy guessed wrong: the next read takes Niadra's
        if self.refused(write, error) is not None:
            self.conflicts.append(
                Conflict(write.scope.model_dump(mode="json"), write.agent, dict(write.body), write.if_version)
            )
            logger.warning("niadra: a working state write sent after an outage met a newer version")


def _key(scope: Mapping[str, str], agent: str) -> tuple[str, str, str]:
    return (str(scope["kind"]), str(scope["id"]), agent)


def _apply(base: Mapping[str, Any], write: AgentStateWrite) -> dict[str, Any]:
    if write.mode == "cas":
        return copy.deepcopy(dict(write.body))
    out = copy.deepcopy(dict(base))
    for name, value in write.body.items():
        if value == DELETE:
            out.pop(name, None)
        else:
            out[name] = copy.deepcopy(value)
    return out


def _read(version: int) -> None:
    frame = current_turn()
    if frame is not None:
        frame.read("agent_state", version=str(version))


class _Handle:
    """What both handles share: the scope, the agent and the copies."""

    def __init__(
        self,
        states: AgentStates,
        scope: Mapping[str, str],
        agent: str | None,
        subject: Handle | None,
        budget: float,
        fail: Callable[[str, Exception, Any], Any],
    ) -> None:
        self._states = states
        self._scope = dict(scope)
        self._agent = agent or "agent"
        self._subject = subject
        self._budget = budget
        self._fail = fail

    @property
    def conflicts(self) -> list[Conflict]:
        """Writes of this scope and agent, sent after an outage, that met a newer version."""
        return [c for c in self._states.conflicts if c.scope == self._scope and c.agent == self._agent]

    def _write(self, body: Mapping[str, Any], mode: Mode, if_version: int | None) -> AgentStateWrite:
        subject = None if self._scope["kind"] == "subject" else self._subject
        return self._states.write(
            self._scope, self._agent, body, mode=mode, if_version=if_version, subject=subject
        )

    def _refused(self, write: AgentStateWrite, error: Exception) -> StateWrite:
        answer = self._states.refused(write, error)
        if answer is not None:
            return answer
        if is_retryable(error):
            return self._states.later(write)
        result: StateWrite = self._fail("agent_state", error, StateWrite(False, 0, error_code(error)))
        return result


class AgentStateHandle(_Handle):
    """`conversation.agent_state` of the sync client."""

    def __init__(self, send: Callable[[Request], Any], *args: Any) -> None:
        super().__init__(*args)
        self._send = send

    def get(self) -> WorkingState:
        """The state of this scope and agent: version 0 and an empty body before the first write."""
        try:
            data = self._send(self._states.read_http(self._scope, self._agent, self._budget))
        except Exception:
            return self._states.unread(self._scope, self._agent)
        return self._states.read(self._scope, self._agent, data)

    def put(
        self, body: Mapping[str, Any], *, mode: Mode = "merge_by_key", if_version: int | None = None
    ) -> StateWrite:
        """Writes the state: `merge_by_key` by default, `cas` at `if_version`. See the module."""
        write = self._write(body, mode, if_version)
        try:
            data = self._send(self._states.write_http(write, self._budget))
        except Exception as error:
            return self._refused(write, error)
        return self._states.written(write, data)


class AsyncAgentStateHandle(_Handle):
    """`conversation.agent_state` of the async client."""

    def __init__(self, send: Callable[[Request], Awaitable[Any]], *args: Any) -> None:
        super().__init__(*args)
        self._send = send

    async def get(self) -> WorkingState:
        """The state of this scope and agent: version 0 and an empty body before the first write."""
        try:
            data = await self._send(self._states.read_http(self._scope, self._agent, self._budget))
        except Exception:
            return self._states.unread(self._scope, self._agent)
        return self._states.read(self._scope, self._agent, data)

    async def put(
        self, body: Mapping[str, Any], *, mode: Mode = "merge_by_key", if_version: int | None = None
    ) -> StateWrite:
        """Writes the state: `merge_by_key` by default, `cas` at `if_version`. See the module."""
        write = self._write(body, mode, if_version)
        try:
            data = await self._send(self._states.write_http(write, self._budget))
        except Exception as error:
            return self._refused(write, error)
        return self._states.written(write, data)
