from __future__ import annotations

from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

# The base comes as a module: Dify loads the one Tool subclass a tool's file defines.
from tools import niadra_base


class RecordReplyTool(niadra_base.NiadraTool):
    """Records the agent's reply for every other agent of the company."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        recorded = False
        said = niadra_base.text(tool_parameters.get("agent_message"))
        if said:
            try:
                recorded = bool(self._conversation(tool_parameters).agent(said))
                self._flush()
            except Exception as exc:
                niadra_base.logger.warning("niadra: could not record the reply (%s)", type(exc).__name__)
        yield self.create_json_message({"recorded": recorded})
