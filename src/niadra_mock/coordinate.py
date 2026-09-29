"""Coordination in the emulator (`spec/coordination.md`, `spec/contact-token.md`), behind `coordination`.

- `POST /v1/coordination/check` decides in the spec's order: a customer's message is allowed; an effect key
  that is done, in flight or of unknown outcome denies, and one free is reserved; a suppression of the purpose
  denies; another holder's claim defers; a spent budget (`budget()`) denies. An allowed outbound contact of a
  purpose that needs a token, through a gateway the space knows (`gateway()`), carries a contact token
  signed with the space's key.
- `POST /v1/coordination/declare` records the declaration; an `effect` settles its key.
- `POST /v1/coordination/claims` holds a claim, 409 `lease_held` or `task_locked` while another holder has
  one on the same subject or task; `/claims/{id}/release` lets it go.
- `GET /.well-known/niadra-contact-keys.json?space=` publishes the space's public key.

Signing needs `cryptography`, as the SDK's gateway check does.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from niadra.coordination.destination import canonical_destination
from niadra.models.coordination import CheckRequest, ClaimRequest

SPACE = UUID("0192f5a0-0000-7000-8000-00000000a0e1")
KID = "ck_mock_space_1"
TOKEN_PURPOSES = frozenset({"marketing", "retention", "collection"})
_SEED = hashlib.sha256(b"niadra-mock-contact-key").digest()


class CoordinationError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class _Claim:
    claim_id: str
    kind: str
    holder: str
    target: tuple[str, ...]
    level: str
    until: datetime
    epoch: int


@dataclass
class CoordinationStore:
    space: UUID = SPACE
    effects: dict[str, dict[str, Any]] = field(default_factory=dict)
    declarations: list[dict[str, Any]] = field(default_factory=list)
    claims: dict[str, _Claim] = field(default_factory=dict)
    budgets: dict[str, int] = field(default_factory=dict)
    spent: dict[tuple[str, str], int] = field(default_factory=dict)
    gateways: dict[str, bytes] = field(default_factory=dict)
    checks: list[CheckRequest] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def budget(self, purpose: str, limit: int) -> None:
        """At most `limit` allowed outbound contacts of `purpose` per subject."""
        self.budgets[purpose] = limit

    def gateway(self, gateway_id: str, key: bytes) -> None:
        """A gateway the space shares a key with: tokens name it, keyed with `key`."""
        self.gateways[gateway_id] = key

    def check(self, request: CheckRequest, suppressed: bool) -> dict[str, Any]:
        with self._lock:
            self.checks.append(request)
            decision, reasons, effect = self._decide(request, suppressed)
            result: dict[str, Any] = {
                "decision": decision,
                "decision_id": str(uuid4()),
                "reasons": reasons,
                "valid_for_s": 60,
            }
            if effect is not None:
                result["effect"] = effect
            if decision == "allow" and request.direction == "outbound" and request.subject is not None:
                if request.purpose in self.budgets:
                    key = (request.purpose, f"{request.subject.type}:{request.subject.value}")
                    self.spent[key] = self.spent.get(key, 0) + 1
                token = self._token(request)
                if token is not None:
                    result["contact_token"] = token
            return result

    def _decide(
        self, request: CheckRequest, suppressed: bool
    ) -> tuple[str, list[str], dict[str, Any] | None]:
        if request.direction == "inbound":
            return "allow", [], None
        effect = None
        if request.effect_key is not None:
            held = self.effects.get(request.effect_key) or {"state": None, "attempt": 0}
            seen = {"done": ("effect_done", "done"), "reserved": ("effect_in_flight", "in_flight")}
            if held["state"] in seen:
                reason, state = seen[held["state"]]
                return "deny", [reason], {"state": state, "attempt": held["attempt"]}
            if held["state"] == "unknown_outcome":
                return "deny", ["effect_unknown_outcome"], {"state": "unknown_outcome"}
            attempt = held["attempt"] + 1
            self.effects[request.effect_key] = {"state": "reserved", "attempt": attempt}
            effect = {"state": "none", "attempt": attempt}
        if suppressed:
            return "deny", ["suppressed"], effect
        subject = f"{request.subject.type}:{request.subject.value}" if request.subject is not None else None
        now = datetime.now(timezone.utc)
        for claim in self.claims.values():
            held_by_other = claim.until > now and claim.holder != request.agent
            if held_by_other and subject is not None and claim.target == ("subject", subject):
                return "defer", ["owner_active"], effect
        limit = self.budgets.get(request.purpose)
        if (
            limit is not None
            and subject is not None
            and self.spent.get((request.purpose, subject), 0) >= limit
        ):
            return "deny", ["budget_exhausted"], effect
        return "allow", [], effect

    def _token(self, request: CheckRequest) -> str | None:
        if request.purpose not in TOKEN_PURPOSES or request.gateway_id not in self.gateways:
            return None
        assert request.subject is not None
        assert request.gateway_id is not None
        assert request.channel is not None
        rcpt = request.destination_hash or _b64(
            hmac.new(
                self.gateways[request.gateway_id],
                canonical_destination(str(request.subject.type), request.subject.value).encode(),
                hashlib.sha256,
            ).digest()
        )
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            "kid": KID,
            "space": str(self.space),
            "jti": str(uuid4()),
            "purpose": request.purpose,
            "channel": request.channel,
            "rcpt": rcpt,
            "gateway": request.gateway_id,
            "iat": now,
            "exp": now + 120,
        }
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        return f"nct1.{payload}.{_b64(_private().sign(f'nct1.{payload}'.encode()))}"

    def declare(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.declarations.append(body)
            if body.get("kind") == "effect":
                detail = body.get("detail", {})
                held = self.effects.setdefault(detail["effect_key"], {"attempt": detail.get("attempt", 1)})
                held["state"] = detail["state"]
        return {"accepted": True}

    def claim(self, request: ClaimRequest) -> dict[str, Any]:
        if request.object is not None:
            obj = request.object
            target: tuple[str, ...] = ("object", f"{obj.type}:{obj.namespace}:{obj.id}", request.task or "")
        elif request.subject is not None:
            target = ("subject", f"{request.subject.type}:{request.subject.value}")
        else:
            raise CoordinationError(422, "invalid_input")
        now = datetime.now(timezone.utc)
        with self._lock:
            for held in self.claims.values():
                if held.target == target and held.until > now and held.holder != request.holder:
                    raise CoordinationError(
                        409, "task_locked" if request.kind == "task_lock" else "lease_held"
                    )
            claim = _Claim(
                f"cl_{uuid4().hex[:12]}",
                request.kind,
                request.holder,
                target,
                request.level or "agent_active",
                now + timedelta(seconds=request.lease_s),
                1,
            )
            self.claims[claim.claim_id] = claim
        return _claim_view(claim)

    def release(self, claim_id: str) -> dict[str, Any]:
        with self._lock:
            claim = self.claims.get(claim_id)
            if claim is None:
                raise CoordinationError(404, "not_found")
            claim.until = datetime.now(timezone.utc)
            claim.epoch += 1
        return _claim_view(claim)

    def keys(self, space: str | None) -> dict[str, Any]:
        if space != str(self.space):
            return {"keys": []}
        public = _private().public_key().public_bytes_raw()
        key = {"kid": KID, "kty": "OKP", "crv": "Ed25519", "x": _b64(public), "space": str(self.space)}
        return {"keys": [{**key, "status": "active"}]}


def _claim_view(claim: _Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "epoch": claim.epoch,
        "holder": claim.holder,
        "kind": claim.kind,
        "level": claim.level,
        "valid_until": claim.until.isoformat(),
    }


def _private() -> Any:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(_SEED)
