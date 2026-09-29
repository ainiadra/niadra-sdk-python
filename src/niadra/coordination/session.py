"""What a conversation or a task declares after it acts (`conversation.declare`): the coordination spec's
declarations, sent in the background with an idempotency key.

```python
conversation.declare.effect(f"farewell:{conversation.id}", "done")
conversation.declare.contact_made(decision, jti=claims.jti, gateway_id="wa_gateway")
conversation.declare("case.opened", level="human_active", lease_s=1800, intents=["support"])
```
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from niadra.coordination.client import Checked, Coordinator, EffectState, effect_detail, report_effect
from niadra.models.coordination import CheckResult

if TYPE_CHECKING:
    from niadra.models.common import Handle, ObjectRef


class Declarations:
    """`conversation.declare`: call it with a kind and its detail, or use the helpers of the common kinds.
    Each returns the declaration's idempotency key; none waits for Niadra."""

    def __init__(
        self,
        coordinator: Coordinator,
        checked: Checked,
        *,
        agent: str | None,
        subject: Handle | None,
        object: ObjectRef | None,
    ) -> None:
        self._coordinator = coordinator
        self._checked = checked
        self._agent = agent or "agent"
        self._subject = subject
        self._object = object

    def __call__(self, kind: str, /, **detail: Any) -> str:
        return self._coordinator.declare(
            kind, detail, agent=self._agent, subject=self._subject, object=self._object
        )

    def effect(self, key: str, state: EffectState = "done", *, attempt: int | None = None) -> str:
        """How the attempt a check reserved for `key` ended. A send whose outcome the agent does not know is
        `unknown_outcome`: it is never sent again on its own."""
        report_effect(key, state)
        return self("effect", **effect_detail(key, state, self._checked, attempt))

    def contact_made(
        self,
        decision: CheckResult | None,
        *,
        purpose: str,
        channel: str,
        gateway_id: str | None = None,
        jti: str | None = None,
    ) -> str:
        """An outbound contact left: `decision` is the check's (None when there was none), `jti` the contact
        token's. A contact that left on a check Niadra could not answer says `unchecked`."""
        unchecked = decision is None or "unchecked" in decision.reasons
        detail: dict[str, Any] = {"purpose": purpose, "channel": channel, "unchecked": unchecked}
        if decision is not None and "unchecked" not in decision.reasons:
            detail["decision_id"] = str(decision.decision_id)
        if gateway_id is not None:
            detail["gateway_id"] = gateway_id
        if jti is not None:
            detail["jti"] = jti
        return self("contact.made", **detail)
