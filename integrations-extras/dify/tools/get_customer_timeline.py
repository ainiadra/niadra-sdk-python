from __future__ import annotations

from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

# The base comes as a module: Dify loads the one Tool subclass a tool's file defines.
from tools import niadra_base


class GetCustomerTimelineTool(niadra_base.NiadraTool):
    """The kit's `get_customer_timeline`, for the customer the app configured."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        arguments: dict[str, Any] = {}
        cursor, when, limit = (
            niadra_base.text(tool_parameters.get("cursor")),
            niadra_base.text(tool_parameters.get("when")),
            None,
        )
        if isinstance(tool_parameters.get("limit"), (int, float)):
            limit = int(tool_parameters["limit"])
        if cursor:
            arguments["cursor"] = cursor
        if limit:
            arguments["limit"] = limit
        if when:
            arguments["filters"] = {"when": when}
        yield from self._kit_call(tool_parameters, "get_customer_timeline", arguments)
