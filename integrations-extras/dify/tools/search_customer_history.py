from __future__ import annotations

from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

# The base comes as a module: Dify loads the one Tool subclass a tool's file defines.
from tools import niadra_base


class SearchCustomerHistoryTool(niadra_base.NiadraTool):
    """The kit's `search_customer_history`, for the customer the app configured."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        arguments: dict[str, Any] = {"query": str(tool_parameters.get("query") or "")}
        when = niadra_base.text(tool_parameters.get("when"))
        if when:
            arguments["filters"] = {"when": when}
        yield from self._kit_call(tool_parameters, "search_customer_history", arguments)
