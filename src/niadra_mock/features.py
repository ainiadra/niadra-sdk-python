"""The agent features in the emulator: the SDK profile, the blocks a read adds by `include`, and the
suppression list, each behind its feature as the server has them.

- `GET /v1/sdk/profile` answers the features `MockCell.features` lists, with the claim contract set by
  `FeatureStore.claim_contract`; with every feature off it answers 404, as the server does.
- `include: ["constraints"]` needs `signals` and returns the block set for the subject by `constrain()`, or
  an empty one; `include: ["state"]` needs `state` and returns the view set by `show()`, or an empty one. A
  block of a feature that is off answers 404, and `coordination` and `budget` answer 501.
- `GET /v1/suppressions` and `/v1/suppressions/salt` need `coordination`; `suppress()` adds an entry keyed
  with the reader's salt, as the server keys it.
- `declare()` adds a type to the registry the profile serves (with the tool bindings of `tool_bindings`), and
  `POST /v1/types/fingerprint` (feature `state`) compares a fingerprint with the declared one, opening a
  drift issue per type as the object type spec, 8.8.3, says.
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
from niadra_mock.state import StateError

BLOCK_FEATURES = {"constraints": "signals", "state": "state"}
# The members of a type the profile serves, as the server summarizes the registry for the SDK.
SUMMARY = (
    *("type", "version", "ownership", "nature", "key", "inputs", "fields", "values", "sources", "union"),
    *("states", "purposes", "readings", "agent_state"),
)
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
    types: list[dict[str, Any]] = field(default_factory=list)
    tool_bindings: list[dict[str, Any]] = field(default_factory=list)
    drift_issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    """The open drift issues, by type: `issue_id`, `field` and `occurrences`."""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def profile(self) -> SdkProfile:
        if not self.features:
            raise FeatureOffError
        return SdkProfile(
            features=sorted(self.features),
            claim_contract=self.claim_contract,
            types=[_summary(t) for t in self.types],
            tool_bindings=[dict(b) for b in self.tool_bindings],
            valid_for_s=PROFILE_TTL_S,
        )

    def declare(self, declared: dict[str, Any]) -> None:
        """Adds `declared` to the type registry, replacing a type of the same name."""
        with self._lock:
            self.types = [t for t in self.types if t.get("type") != declared["type"]] + [dict(declared)]

    def type_fingerprint(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /v1/types/fingerprint`: drift when the fingerprint differs from the declared one."""
        self.need("state")
        with self._lock:
            declared = next((t for t in self.types if t.get("type") == body.get("type")), None)
            if declared is None:
                raise FeatureOffError
            mirror = declared.get("mirror_of") or {}
            if not mirror.get("fingerprint"):
                raise StateError(422, "no_fingerprint")
            if body["fingerprint"] == mirror["fingerprint"]:
                return {"drift": False, "issue_id": None}
            if mirror.get("drift", "alert") == "ignore":
                return {"drift": True, "issue_id": None}
            named = list((body.get("changes") or {}).get("fields") or [])
            issue = self.drift_issues.get(declared["type"])
            if issue is None:
                issue = {"issue_id": f"di_{len(self.drift_issues) + 1}", "occurrences": 0}
                self.drift_issues[declared["type"]] = issue
            issue["occurrences"] += 1
            issue["field"] = named[0] if len(named) == 1 else None
            return {"drift": True, "issue_id": issue["issue_id"]}

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


def _summary(declared: dict[str, Any]) -> dict[str, Any]:
    out = {k: declared[k] for k in SUMMARY if k in declared}
    if "fields" in out:
        out["fields"] = {n: {k: v for k, v in f.items() if k != "access"} for n, f in out["fields"].items()}
    return out
