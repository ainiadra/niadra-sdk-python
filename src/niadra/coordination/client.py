"""Coordination in the agent's process: ask before acting, declare after (`spec/coordination.md`).

```python
decision = conversation.check("farewell", purpose="service", effect_key=f"farewell:{conversation.id}")
if decision.decision == "allow" and decision.effect and decision.effect.state == "none":
    send(message)
    conversation.declare.effect(f"farewell:{conversation.id}", "done")
```

`check()` answers within its own budget (200 ms by default). When Niadra does not answer in time, the
decision comes from the purpose's direction (the coordination spec, 10; the law that every policy says which
way it fails):

- a message the customer sent (`direction="inbound"`) is never held: `allow`, `unchecked`;
- any purpose whose opt-out the local copy of the suppression list holds: `deny`, `suppressed` (each check
  about an outbound contact keeps that copy, read in the background once a minute);
- an effect with a key: `defer`, `unavailable`, and what may have gone out is never sent again on its own;
- a purpose that fails closed (by default `marketing`, `retention` and `collection`, the ones a gateway
  refuses without a token): `defer`, `unavailable`;
- any other purpose (`transactional`, `service`...): `allow`, `unchecked`, and the declaration says so.

A check the API refuses (400, 401, 403 or 422: a purpose or channel the space does not declare, a key without
the `coordinate` scope) is the integration's error, not an outage: an outbound contact gets `defer` with the
reason `invalid_request` (an inbound message is never held: `allow`), the problem is logged with its request
id, and `strict=True` raises it.

`fail_open=` overrides the purpose's direction when Niadra did not answer. Declarations
(`conversation.declare`) leave in the background with an idempotency key and are sent again until Niadra takes
them; a turn records its decisions and the effects it reported (`coordination` and `effects` in the record).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from niadra._ids import new_key
from niadra._outbox import Outbox, Write
from niadra._transport import Request
from niadra.coordination.suppression import SuppressionCopy
from niadra.errors import APIError, NotFoundError
from niadra.models.common import Handle
from niadra.models.coordination import CheckRequest, CheckResult, ClaimRequest, OwnershipClaim
from niadra.turns.capture import current_turn

if TYPE_CHECKING:
    from niadra.models.common import ObjectRef

logger = logging.getLogger("niadra")

REFUSED = frozenset({400, 401, 403, 422})
"""Statuses of a check the API refused as asked: the integration's error, never an outage."""
FAIL_CLOSED = frozenset({"marketing", "retention", "collection"})
"""Purposes that wait when Niadra cannot decide; every other purpose goes, marked unchecked."""
CHECK_BUDGET = 0.200
"""Seconds a check may take before the purpose's direction decides."""

EffectState = Literal["done", "failed", "unknown_outcome"]


def check_request(
    *,
    agent: str,
    intent: str,
    purpose: str,
    direction: Literal["inbound", "outbound"],
    subject: Handle | None,
    channel: str | None,
    effect_key: str | None = None,
    effect_kind: str | None = None,
    object: ObjectRef | None = None,
    task: str | None = None,
    gateway_id: str | None = None,
    destination_hash: str | None = None,
) -> CheckRequest:
    fields: dict[str, Any] = {
        "agent": agent,
        "intent": intent,
        "purpose": purpose,
        "direction": direction,
        "subject": subject,
        "channel": channel,
        "effect_key": effect_key,
        "effect_kind": effect_kind,
        "object": object,
        "task": task,
        "gateway_id": gateway_id,
        "destination_hash": destination_hash,
    }
    return CheckRequest.model_validate({k: v for k, v in fields.items() if v is not None})


def refused(error: Exception | None) -> bool:
    """Whether the API refused the check as asked (`REFUSED`)."""
    return isinstance(error, APIError) and error.status_code in REFUSED


def fallback(
    *,
    direction: str,
    purpose: str,
    effect_key: str | None = None,
    suppressed: bool = False,
    fail_open: bool | None = None,
    reason: str = "unavailable",
) -> CheckResult:
    """The decision when Niadra did not answer in time, by the purpose's direction. Nothing is reserved and
    no token is issued."""
    decision: Literal["allow", "defer", "deny"]
    if direction == "inbound":
        decision, reasons = "allow", ["unchecked"]
    elif suppressed:
        decision, reasons = "deny", ["suppressed"]
    elif effect_key is not None:
        decision, reasons = "defer", [reason]
    elif fail_open if fail_open is not None else purpose not in FAIL_CLOSED:
        decision, reasons = "allow", ["unchecked"]
    else:
        decision, reasons = "defer", [reason]
    return CheckResult(decision=decision, decision_id=uuid4(), reasons=reasons, valid_for_s=0)


@dataclass
class Checked:
    """What a session remembers of the checks it made: the attempt a check reserved for each effect key."""

    attempts: dict[str, int] = field(default_factory=dict)
    unchecked: dict[str, str] = field(default_factory=dict)
    """The decision id of a check that went unchecked, by effect key or intent: its declaration says so."""


class Coordinator:
    """What a client keeps for coordination: the write outbox, and the local suppression copy the fallback
    reads."""

    def __init__(self, outbox: Outbox, suppressions: SuppressionCopy) -> None:
        self.outbox = outbox
        self.suppressions = suppressions

    def suppressed(self, request: CheckRequest) -> bool:
        if request.subject is None or request.direction == "inbound":
            return False
        target = request.subject.model_dump(mode="json")
        return not self.suppressions.may_contact(
            target, request.purpose, channel=request.channel, fail_open=True
        )

    def check_http(self, request: CheckRequest, budget: float) -> Request:
        body = request.model_dump(mode="json", exclude_none=True)
        return Request(
            "POST", "/v1/coordination/check", json=body, timeout=budget, budget=budget, max_attempts=1
        )

    def decided(self, data: Any, request: CheckRequest, checked: Checked) -> CheckResult:
        result = CheckResult.model_validate(data)
        _record(result)
        if request.effect_key is not None and result.effect is not None and result.effect.state == "none":
            checked.attempts[request.effect_key] = result.effect.attempt or 1
        return result

    def failed(
        self, request: CheckRequest, checked: Checked, fail_open: bool | None, error: Exception | None = None
    ) -> CheckResult:
        """The purpose's direction decides. A space that does not coordinate (404) holds nothing back. A check
        the API refused holds an outbound contact back, as `invalid_request`."""
        if refused(error):
            assert isinstance(error, APIError)
            logger.warning("niadra: the coordination check was refused: %s", error.explained())
            decision: Literal["allow", "defer"] = "allow" if request.direction == "inbound" else "defer"
            invalid = CheckResult(
                decision=decision, decision_id=uuid4(), reasons=["invalid_request"], valid_for_s=0
            )
            _record(invalid)
            return invalid
        if isinstance(error, NotFoundError):
            fail_open = True
        result = fallback(
            direction=request.direction,
            purpose=request.purpose,
            effect_key=None if isinstance(error, NotFoundError) else request.effect_key,
            suppressed=self.suppressed(request),
            fail_open=fail_open,
        )
        if "unchecked" in result.reasons:
            checked.unchecked[request.effect_key or request.intent] = str(result.decision_id)
        _record(result)
        return result

    def declare(
        self,
        kind: str,
        detail: Mapping[str, Any],
        *,
        agent: str,
        subject: Handle | None,
        object: ObjectRef | None = None,
        settled: Callable[[Any, Exception | None], None] | None = None,
    ) -> str:
        """Queues one declaration; returns its idempotency key."""
        body: dict[str, Any] = {"kind": kind, "agent": agent, "detail": dict(detail)}
        if subject is not None:
            body["subject"] = subject.model_dump(mode="json", exclude_none=True)
        if object is not None:
            body["object"] = object.model_dump(mode="json")
        key = new_key()
        request = Request("POST", "/v1/coordination/declare", json=body, idempotency_key=key)
        self.outbox.put(Write(request, settled))
        return key

    @staticmethod
    def claim_http(request: ClaimRequest, budget: float) -> Request:
        body = request.model_dump(mode="json", exclude_none=True)
        return Request(
            "POST",
            "/v1/coordination/claims",
            json=body,
            timeout=budget,
            budget=budget,
            idempotency_key=new_key(),
        )


@dataclass(frozen=True)
class Claimed:
    """A claim's outcome: `claim` when it is held; otherwise `error`, the code of why not: `lease_held` or
    `task_locked` when another holder has it (a check names who), or why Niadra could not say."""

    claim: OwnershipClaim | None = None
    error: str | None = None

    @property
    def held(self) -> bool:
        return self.claim is not None


def claimed(data: Any = None, error: Exception | None = None) -> Claimed:
    if error is None:
        return Claimed(OwnershipClaim.model_validate(data))
    return Claimed(error=getattr(error, "code", None) or type(error).__name__)


def effect_detail(key: str, state: EffectState, checked: Checked, attempt: int | None) -> dict[str, Any]:
    return {"effect_key": key, "state": state, "attempt": attempt or checked.attempts.get(key, 1)}


def report_effect(key: str, state: EffectState) -> None:
    """The effect as the turn saw it, in the record's `effects`."""
    frame = current_turn()
    if frame is not None:
        frame.effect(key, state)


def _record(result: CheckResult) -> None:
    frame = current_turn()
    if frame is not None:
        frame.coordinate(str(result.decision_id), result.decision)
