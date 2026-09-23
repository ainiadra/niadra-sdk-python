"""`POST /v1/subject-tokens`: binds an MCP connection to one customer.

The token is signed by the cell and lives 15 minutes. It carries the space, the source, the
subject, the conversation and the verification level, so the MCP tools never take a subject
as an argument and the model cannot switch customers mid-conversation.
"""

from __future__ import annotations

from datetime import datetime

from niadra.models._base import IdStr, Model, ResponseModel
from niadra.models.common import Handle
from niadra.vocabulary import Verification

SUBJECT_TOKEN_HEADER = "Niadra-Subject-Token"  # noqa: S105  a header name, not a secret


class SubjectTokenRequest(Model):
    subject: Handle
    about: Handle | None = None
    conversation_id: IdStr | None = None
    task_id: IdStr | None = None
    verification: Verification = Verification.V0


class SubjectToken(ResponseModel):
    token: str
    expires_at: datetime

    @property
    def headers(self) -> dict[str, str]:
        """The header to send when opening the MCP connection, next to the source key."""
        return {SUBJECT_TOKEN_HEADER: self.token}
