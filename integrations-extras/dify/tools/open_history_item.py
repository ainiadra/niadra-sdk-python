from __future__ import annotations

from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

# The base comes as a module: Dify loads the one Tool subclass a tool's file defines.
from tools import niadra_base


class OpenHistoryItemTool(niadra_base.NiadraTool):
    """The kit's `open_history_item`, for the customer the app configured."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        arguments = {"id": str(tool_parameters.get("id") or "")}
        yield from self._kit_call(tool_parameters, "open_history_item", arguments)
