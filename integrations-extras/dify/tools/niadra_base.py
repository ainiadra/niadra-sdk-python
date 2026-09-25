"""What the Niadra tools of this plugin share: the client, the conversation, and the kit call.

Every tool reads the customer from its form parameters (set by the app builder, usually from the
app's variables), never from the model. Niadra slow or down never fails the tool: the context comes
out empty and a history tool answers that the history is unavailable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator, Mapping
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from niadra import Niadra, app_user, email, phone, system_id, whatsapp

logger = logging.getLogger("niadra")

HANDLES = {
    "phone": phone,
    "whatsapp": whatsapp,
    "email": email,
    "app_user": app_user,
    "system_id": lambda value: system_id(*value.split(":", 1)),
}

# Recorded turns leave in the background; a tool waits this long for them before it returns.
FLUSH_SECONDS = 2.0

_CLIENTS: dict[tuple[str, str], Niadra] = {}


def client(api_key: str, base_url: str | None) -> Niadra:
    """One client per process and key, as the SDK recommends; tests replace this factory."""
    key = (api_key, base_url or "")
    found = _CLIENTS.get(key)
    if found is None:
        found = _CLIENTS[key] = Niadra(api_key, base_url=base_url or None)
    return found


def text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class NiadraTool(Tool):
    """A Dify tool bound to one Niadra conversation, from the tool's form parameters."""

    def _client(self) -> Niadra:
        credentials: Mapping[str, Any] = self.runtime.credentials
        return client(str(credentials.get("api_key") or ""), text(credentials.get("base_url")))

    def _conversation(self, parameters: Mapping[str, Any], view: str = "chat") -> Any:
        make = HANDLES.get(str(parameters.get("customer_id_type") or "phone"), phone)
        customer = text(parameters.get("customer_id"))
        try:
            subject = make(customer) if customer else None
        except ValueError:
            subject = None
        return self._client().conversation(
            text(parameters.get("conversation_id")),
            subject=subject,
            channel=text(parameters.get("channel")) or "chat",
            view=view,
        )

    def _flush(self) -> None:
        try:
            self._client().flush(FLUSH_SECONDS)
        except Exception as exc:
            logger.warning("niadra: could not flush (%s)", type(exc).__name__)

    def _kit_call(
        self,
        parameters: Mapping[str, Any],
        name: str,
        arguments: dict[str, Any],
        *,
        agent_memory: bool = False,
    ) -> Generator[ToolInvokeMessage, None, None]:
        """Runs one kit tool for the customer and yields its result as text and as JSON."""
        try:
            kit = self._conversation(parameters).tools(agent_memory=agent_memory)
        except Exception as exc:
            logger.warning("niadra: could not build the history tools (%s)", type(exc).__name__)
            kit = None
        if kit is None:
            result = json.dumps({"error": "no customer in this app; answer without the history"})
        else:
            result = kit.call(name, arguments)
        yield self.create_text_message(result)
        try:
            parsed = json.loads(result)
        except ValueError:
            return
        if isinstance(parsed, dict):
            yield self.create_json_message(parsed)
