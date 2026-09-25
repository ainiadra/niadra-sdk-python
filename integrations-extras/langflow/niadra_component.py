"""Niadra components for Langflow: the customer's memory in a flow.

Copy this file into your Langflow components folder (or paste it into a custom component) and
install the SDK where Langflow runs: `pip install niadra`. Three components:

- **Niadra Context** records the customer's message and outputs the context for the prompt: the
  agent's own notes (with "Agent memory" on), the customer's pinned pack and what changed on other
  channels. Connect it to the system prompt of your agent or prompt template, after your own
  instructions.
- **Niadra Record Reply** records the agent's reply and passes it on unchanged.
- **Niadra Search History** searches the customer's whole history. In tool mode the agent calls
  it with a query (and optionally a time phrase); the customer is the one the flow configured,
  never a value the model chooses.

The three take the same connection fields: the source key, the channel, the customer's handle and
the conversation id, usually mapped from the flow's inputs. Niadra slow or down never fails the
flow: the context comes out empty and the search says the history is unavailable.
"""

from __future__ import annotations

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DropdownInput, MessageInput, MessageTextInput, Output, SecretStrInput, StrInput
from lfx.schema.message import Message

from niadra import Niadra, app_user, email, phone, system_id, whatsapp
from niadra.tools import BUILTIN_DEFINITIONS

HANDLES = {
    "phone": phone,
    "whatsapp": whatsapp,
    "email": email,
    "app_user": app_user,
    "system_id": lambda value: system_id(*value.split(":", 1)),
}
VIEWS = ["chat", "voice", "brief", "full"]
SEARCH_DESCRIPTION = next(
    d["function"]["description"]
    for d in BUILTIN_DEFINITIONS
    if d["function"]["name"] == "search_customer_history"
)


def join_instructions(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def _client(api_key: str, base_url: str | None, channel: str) -> Niadra:
    """One client per process and key, as the SDK recommends; tests replace this factory."""
    key = (api_key, base_url or "", channel)
    client = _CLIENTS.get(key)
    if client is None:
        client = _CLIENTS[key] = Niadra(api_key or None, base_url=base_url or None, channel=channel)
    return client


_CLIENTS: dict[tuple[str, str, str], Niadra] = {}


def _connection() -> list[Any]:
    return [
        SecretStrInput(name="api_key", display_name="Niadra source key", required=True),
        StrInput(
            name="base_url", display_name="API address", advanced=True, info="Only for the local emulator."
        ),
        StrInput(name="channel", display_name="Channel", value="chat"),
        DropdownInput(
            name="handle_type", display_name="Customer id type", options=list(HANDLES), value="phone"
        ),
        MessageTextInput(
            name="handle_value", display_name="Customer id", info="E.164 phone, wa_id, e-mail..."
        ),
        MessageTextInput(name="conversation_id", display_name="Conversation id"),
        DropdownInput(name="view", display_name="View", options=VIEWS, value="chat", advanced=True),
    ]


class _NiadraBase(Component):  # type: ignore[misc]
    def _conversation(self) -> Any:
        client = _client(self.api_key, getattr(self, "base_url", None), self.channel or "chat")
        make = HANDLES.get(self.handle_type or "phone", phone)
        try:
            subject = make(str(self.handle_value).strip()) if self.handle_value else None
        except ValueError:
            subject = None
        return client.conversation(
            str(self.conversation_id) if self.conversation_id else None,
            subject=subject,
            channel=self.channel or "chat",
            view=self.view or "chat",
        )


class NiadraContextComponent(_NiadraBase):
    display_name = "Niadra Context"
    description = "Records the customer's message and outputs what the company knows about the customer."
    documentation = "https://docs.niadra.com/en"
    icon = "brain"
    name = "NiadraContext"

    inputs = [
        *_connection(),
        MessageInput(name="customer_message", display_name="Customer message", required=False),
        BoolInput(name="agent_memory", display_name="Agent memory", value=False, advanced=True),
    ]
    outputs = [Output(display_name="Context", name="context", method="build_context")]

    def build_context(self) -> Message:
        conversation = self._conversation()
        said = getattr(self.customer_message, "text", self.customer_message)
        if isinstance(said, str) and said.strip():
            conversation.customer(said)
        notes = ""
        if self.agent_memory:
            block = conversation.agent_memory()
            notes = block.text if block.enabled else ""
        context = conversation.context()
        text = join_instructions(notes, context.system_block, context.turn_block)
        if context.system_block or context.turn_block:
            conversation.mark_injected(context)
        self.status = text
        return Message(text=text)


class NiadraRecordReplyComponent(_NiadraBase):
    display_name = "Niadra Record Reply"
    description = "Records the agent's reply for every other agent of the company, and passes it on."
    documentation = "https://docs.niadra.com/en"
    icon = "brain"
    name = "NiadraRecordReply"

    inputs = [*_connection(), MessageInput(name="agent_message", display_name="Agent reply", required=True)]
    outputs = [Output(display_name="Reply", name="reply", method="record_reply")]

    def record_reply(self) -> Message:
        said = getattr(self.agent_message, "text", self.agent_message)
        if isinstance(said, str) and said.strip():
            self._conversation().agent(said)
        return (
            self.agent_message if isinstance(self.agent_message, Message) else Message(text=str(said or ""))
        )


class NiadraSearchHistoryComponent(_NiadraBase):
    display_name = "Niadra Search History"
    # In tool mode this is the tool's description: the kit's own words, as in every Niadra SDK.
    description = SEARCH_DESCRIPTION
    documentation = "https://docs.niadra.com/en"
    icon = "brain"
    name = "NiadraSearchHistory"

    inputs = [
        *_connection(),
        MessageTextInput(
            name="query",
            display_name="Query",
            info="What to look for, in the customer's words.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="when",
            display_name="When",
            info="The period in the customer's own words: 'last week', 'semana passada', 'en marzo'.",
            tool_mode=True,
            required=False,
        ),
    ]
    outputs = [Output(display_name="Results", name="results", method="search_history")]

    def search_history(self) -> Message:
        kit = self._conversation().tools()
        if kit is None:
            return Message(text='{"error": "no customer in this flow"}')
        arguments: dict[str, Any] = {"query": str(self.query or "")}
        if self.when:
            arguments["filters"] = {"when": str(self.when)}
        text = kit.call("search_customer_history", arguments)
        self.status = text
        return Message(text=text)
