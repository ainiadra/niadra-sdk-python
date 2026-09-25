from __future__ import annotations

from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

# The base comes as a module: Dify loads the one Tool subclass a tool's file defines.
from tools import niadra_base


class GetContextTool(niadra_base.NiadraTool):
    """Records the customer's message and returns the context for the prompt: the agent's own notes
    (with "Agent memory" on), the customer's pinned pack and what changed on other channels."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        blocks: list[str] = []
        etag = None
        try:
            conversation = self._conversation(
                tool_parameters, view=niadra_base.text(tool_parameters.get("view")) or "chat"
            )
            said = niadra_base.text(tool_parameters.get("customer_message"))
            if said:
                conversation.customer(said)
            if tool_parameters.get("agent_memory"):
                notes = conversation.agent_memory()
                if notes.enabled and notes.text:
                    blocks.append(notes.text)
            context = conversation.context()
            blocks += [block for block in (context.system_block, context.turn_block) if block]
            if context.system_block or context.turn_block:
                conversation.mark_injected(context)
                etag = context.etag
            self._flush()
        except Exception as exc:
            niadra_base.logger.warning("niadra: could not read the context (%s)", type(exc).__name__)
        joined = "\n\n".join(blocks)
        yield self.create_text_message(joined)
        yield self.create_json_message({"context": joined, "etag": etag})
