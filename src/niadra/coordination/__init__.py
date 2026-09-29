"""Coordination in the agent's process: ask before acting, declare after, and check a contact where the
message leaves.

- `niadra.coordination.client`: the check with each purpose's direction when Niadra cannot decide, claims and
  task locks; `conversation.check()`, `conversation.claim()` and `conversation.declare`.
- `niadra.coordination.token`: the contact token's offline check at the company's gateway.
- `niadra.coordination.suppression`: the local copy of the suppression list, which keeps an opt-out with
  Niadra down.
- `niadra.coordination.destination`: a handle's canonical destination and its key per reader, which the
  suppression list and the contact token share.
"""

from niadra.coordination.token import (
    AsyncContactGateway,
    ContactClaims,
    ContactGateway,
    ContactTokenError,
    recipient_hash,
    verify_contact_token,
)

__all__ = [
    "AsyncContactGateway",
    "ContactClaims",
    "ContactGateway",
    "ContactTokenError",
    "recipient_hash",
    "verify_contact_token",
]
