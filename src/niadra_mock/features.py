"""The agent features in the emulator: the SDK profile, the blocks a read adds by `include`, and the
suppression list, each behind its feature as the server has them.

- `GET /v1/sdk/profile` answers the features `MockCell.features` lists, with the claim contract set by
  `FeatureStore.claim_contract`; with every feature off it answers 404, as the server does.
- `include: ["constraints"]` needs `signals` and returns the block set for the subject by `constrain()`, or
  an empty one; `include: ["state"]` needs `state` and returns the view set by `show()`, or an empty one. A
  block of a feature that is off answers 404, and `coordination` and `budget` answer 501.
- `GET /v1/suppressions` and `/v1/suppressions/salt` need `coordination`; `suppress()` adds an entry keyed
  with the reader's salt, as the server keys it.
"""

from __future__ import annotations

import base64
import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from niadra.coordination.destination import canonical_destination, suppression_key
from niadra.models.common import Handle
from niadra.models.coordination import Suppression, SuppressionPage, SuppressionSalt
from niadra.models.signals import ConstraintsBlock
from niadra.models.state import ClaimContractSummary, SdkProfile, StateView

BLOCK_FEATURES = {"constraints": "signals", "state": "state"}
PROFILE_TTL_S = 300


class FeatureOffError(Exception):
    """A block or route of a feature the space left off: 404."""


class NotBuiltError(Exception):
    """A block the emulator does not provide: 501."""


@dataclass
class FeatureStore:
    features: set[str]
    claim_contract: ClaimContractSummary | None = None
    salt: bytes = b"niadra-mock-suppression-salt-32b"
    blocks: dict[tuple[str, str], ConstraintsBlock] = field(default_factory=dict)
    views: dict[tuple[str, str], StateView] = field(default_factory=dict)
    suppressions: list[Suppression] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def profile(self) -> SdkProfile:
        if not self.features:
            raise FeatureOffError
        return SdkProfile(
            features=sorted(self.features),
            claim_contract=self.claim_contract,
            valid_for_s=PROFILE_TTL_S,
        )

    def constrain(self, handle: Handle, block: ConstraintsBlock | dict[str, Any]) -> None:
        """The constraints block a read about `handle` returns with `include: ["constraints"]`."""
        with self._lock:
            self.blocks[(handle.type, handle.value)] = ConstraintsBlock.model_validate(block)

    def show(self, handle: Handle, view: StateView | dict[str, Any]) -> None:
        """The state view a read about `handle` returns with `include: ["state"]`."""
        with self._lock:
            self.views[(handle.type, handle.value)] = StateView.model_validate(view)

    def blocks_for(self, include: list[str], subject: Handle | None) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for name in include:
            feature = BLOCK_FEATURES.get(name)
            if feature is None:
                raise NotBuiltError
            if feature not in self.features:
                raise FeatureOffError
        if "constraints" in include:
            key = (subject.type, subject.value) if subject is not None else None
            with self._lock:
                block = self.blocks.get(key) if key is not None else None
            found["constraints"] = block or ConstraintsBlock(version="cv_" + "0" * 16)
        if "state" in include:
            key = (subject.type, subject.value) if subject is not None else None
            with self._lock:
                view = self.views.get(key) if key is not None else None
            found["state"] = view or StateView()
        return found

    def suppress(
        self,
        handle: Handle,
        purpose: str,
        *,
        channel: str | None = None,
        until: datetime | None = None,
    ) -> str:
        """Adds a suppression of `handle` for `purpose`; returns the entry's id."""
        key = suppression_key(self._salt_b64(), canonical_destination(str(handle.type), handle.value))
        with self._lock:
            entry = Suppression(
                id=f"s{len(self.suppressions) + 1}",
                key=key,
                purpose=purpose,
                channel=channel,
                since=datetime.now(timezone.utc),
                until=until,
            )
            self.suppressions.append(entry)
        return entry.id

    def suppressed(self, handle: Handle, purpose: str, channel: str | None) -> bool:
        """Whether an entry of the list forbids contacting `handle` for `purpose` on `channel` now."""
        try:
            key = suppression_key(self._salt_b64(), canonical_destination(str(handle.type), handle.value))
        except ValueError:
            return False
        now = datetime.now(timezone.utc)
        with self._lock:
            return any(
                e.key == key
                and e.purpose == purpose
                and e.channel in (None, channel)
                and e.since <= now
                and (e.until is None or e.until > now)
                for e in self.suppressions
            )

    def suppression_page(self) -> SuppressionPage:
        self.need("coordination")
        with self._lock:
            return SuppressionPage(items=list(self.suppressions), salt_id=self._salt_id())

    def suppression_salt(self) -> SuppressionSalt:
        self.need("coordination")
        return SuppressionSalt(
            salt=self._salt_b64(),
            salt_id=self._salt_id(),
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def need(self, feature: str) -> None:
        if feature not in self.features:
            raise FeatureOffError

    def _salt_b64(self) -> str:
        return base64.urlsafe_b64encode(self.salt).rstrip(b"=").decode()

    def _salt_id(self) -> str:
        return "salt-" + hashlib.sha256(self.salt).hexdigest()[:8]
