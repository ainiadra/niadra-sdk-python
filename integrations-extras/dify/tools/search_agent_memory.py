from __future__ import annotations

from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

# The base comes as a module: Dify loads the one Tool subclass a tool's file defines.
from tools import niadra_base


class SearchAgentMemoryTool(niadra_base.NiadraTool):
    """The kit's `search_agent_memory`: the agent's own notes, never anything about a customer."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        arguments = {"query": str(tool_parameters.get("query") or "")}
        yield from self._kit_call(tool_parameters, "search_agent_memory", arguments, agent_memory=True)
