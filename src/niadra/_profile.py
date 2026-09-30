"""The SDK profile in the client's warm cache: `GET /v1/sdk/profile`.

The profile says which agent features the space turned on, and carries what the SDK checks locally: the claim
contract, the summarized type registry and the bindings of this source's tools. It is read on first need, off
the agent's path when it can be (the turn sender reads it before it builds records), and again once
`valid_for_s` has passed.

A server that does not answer keeps the last profile in use, however old: Niadra being down never turns what
the SDK knew into "not known".
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from niadra._transport import Request
from niadra.models.state import ClaimContractSummary, SdkProfile


class ProfileCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.profile: SdkProfile | None = None
        self._bindings: dict[str, dict[str, Any]] = {}
        self._fetched_at: float | None = None
        self.claim_contract: ClaimContractSummary | None = None
        """A contract of the company's own, which wins over the profile's (for CI and local runs)."""

    def due(self) -> bool:
        """Whether a read should ask the server: no profile yet, or one past `valid_for_s`."""
        with self._lock:
            if self.profile is None or self._fetched_at is None:
                return True
            return self._clock() - self._fetched_at >= self.profile.valid_for_s

    @staticmethod
    def request(timeout: float) -> Request:
        return Request("GET", "/v1/sdk/profile", timeout=timeout, budget=timeout)

    def absorb(self, data: Any) -> SdkProfile:
        profile = SdkProfile.model_validate(data)
        bindings = {b.tool: b.model_dump(mode="json") for b in profile.tool_bindings}
        with self._lock:
            self.profile, self._fetched_at, self._bindings = profile, self._clock(), bindings
        return profile

    @property
    def features(self) -> frozenset[str] | None:
        """The features the space turned on, or None while the SDK does not know."""
        with self._lock:
            return frozenset(self.profile.features) if self.profile is not None else None

    def contract(self) -> ClaimContractSummary | None:
        """The claim contract the SDK checks outputs against: the company's own, else the profile's."""
        if self.claim_contract is not None:
            return self.claim_contract
        with self._lock:
            return self.profile.claim_contract if self.profile is not None else None

    def field_access(self) -> dict[str, dict[str, str]] | None:
        """The fields each type hides from this key (`mask` or `deny`), by type; None while no profile was
        ever read. When Niadra does not answer, the last profile read keeps applying."""
        with self._lock:
            if self.profile is None:
                return None
            return {t["type"]: dict(t.get("field_access") or {}) for t in self.profile.types}

    def tool_binding(self, tool: str) -> dict[str, Any] | None:
        """The binding the space serves this source for `tool`, as `niadra.constraints.binding` reads it; None
        when it binds no such tool or no profile was ever read. The last profile read keeps applying."""
        with self._lock:
            return self._bindings.get(tool)

    def families(self) -> dict[str, str]:
        """Each field's attribute family (`item_variant.size_label` to `size`), from the type registry."""
        with self._lock:
            types = self.profile.types if self.profile is not None else []
        return {
            f"{t['type']}.{name}": spec["attribute"]["family"]
            for t in types
            for name, spec in (t.get("fields") or {}).items()
            if (spec.get("attribute") or {}).get("family")
        }

    def recording_mode(self) -> str | None:
        """The content mode the space's recording names for this source, when the profile says it."""
        with self._lock:
            recording = self.profile.recording if self.profile is not None else None
        return recording.content_mode if recording is not None else None

    def required_pins(self) -> tuple[str, ...]:
        """The pins the space's recording needs for a turn to be replayable, when the profile says them."""
        with self._lock:
            recording = self.profile.recording if self.profile is not None else None
        return tuple(recording.required_pins) if recording is not None else ()
