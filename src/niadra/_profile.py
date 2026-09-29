"""The SDK profile in the client's warm cache: `GET /v1/sdk/profile`.

The profile says which agent features the space turned on, and carries what the SDK checks locally: the
claim contract and the summarized type registry. It is read on first need, off the agent's path when it can
be (the turn sender reads it before it builds records), and again once `valid_for_s` has passed.

- A server that answers 404 has no profile for this key (an older cell, or a space with every feature
  off): the features count as off for 10 minutes, then the SDK asks again, as it does for prefetch.
- A server that does not answer keeps the last profile in use, however old: Niadra being down never turns
  what the SDK knew into "not known".
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from niadra._transport import Request
from niadra.errors import NotFoundError
from niadra.models.state import ClaimContractSummary, SdkProfile

OFF_FOR = 600.0
"""Seconds the features count as off after a 404, before the SDK asks again."""


class ProfileCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.profile: SdkProfile | None = None
        self._fetched_at: float | None = None
        self._off_until = 0.0
        self.claim_contract: ClaimContractSummary | None = None
        """A contract of the company's own, which wins over the profile's (for CI and local runs)."""

    def due(self) -> bool:
        """Whether a read should ask the server: no profile yet, or one past `valid_for_s`."""
        with self._lock:
            now = self._clock()
            if now < self._off_until:
                return False
            if self.profile is None or self._fetched_at is None:
                return True
            return now - self._fetched_at >= self.profile.valid_for_s

    @staticmethod
    def request(timeout: float) -> Request:
        return Request("GET", "/v1/sdk/profile", timeout=timeout, budget=timeout)

    def absorb(self, data: Any) -> SdkProfile:
        profile = SdkProfile.model_validate(data)
        with self._lock:
            self.profile, self._fetched_at = profile, self._clock()
        return profile

    def failed(self, error: BaseException) -> None:
        """A 404 turns the features off for a while; any other failure keeps the last profile."""
        if isinstance(error, NotFoundError):
            with self._lock:
                self.profile, self._fetched_at = None, None
                self._off_until = self._clock() + OFF_FOR

    @property
    def features(self) -> frozenset[str] | None:
        """The features the space turned on, or None while the SDK does not know."""
        with self._lock:
            if self.profile is None:
                return frozenset() if self._clock() < self._off_until else None
            return frozenset(self.profile.features)

    def contract(self) -> ClaimContractSummary | None:
        """The claim contract the SDK checks outputs against: the company's own, else the profile's."""
        if self.claim_contract is not None:
            return self.claim_contract
        with self._lock:
            return self.profile.claim_contract if self.profile is not None else None

    def recording_mode(self) -> str | None:
        """The content mode the space's recording names for this source, when the profile says it."""
        with self._lock:
            recording = self.profile.recording if self.profile is not None else None
        mode = (recording or {}).get("content_mode")
        return mode if mode in ("stored", "pointer", "hash_only") else None
