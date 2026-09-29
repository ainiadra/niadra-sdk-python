"""Field access at a tool's output: a field the agent may not read never reaches its model.

The type registry says, per field, who may read it (`access`). The SDK profile tells each key which fields
of each type it may not read, and `mask_output=True` removes the denied ones and masks the others in what
the tool returns, before the model sees it. The turn records what the model got. When Niadra is down the last
profile read keeps applying; with none ever read, `on_unknown="block"` withholds the result instead.

Needs the space's `state` feature and a type whose fields declare `access`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from niadra import Niadra


def _shown(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"ref": f"proposal:crm:{result['proposal_id']}", "fields": {"price_full": result["price_full"]}}]


def proposal_tool(niadra: Niadra) -> Callable[[str], dict[str, Any]]:
    """The CRM read as the agent's tool, bound to this client's key and its SDK profile."""

    @niadra.tool("get_proposal", provenance=_shown, mask_output=True, on_unknown="block")
    def get_proposal(proposal_id: str) -> dict[str, Any]:
        """The company's CRM read, with every field it holds."""
        return {"proposal_id": proposal_id, "price_full": 812.4, "health_declaration": "(from the CRM)"}

    return get_proposal


if __name__ == "__main__":
    niadra = Niadra(channel="voice")
    niadra.profile()
    print(proposal_tool(niadra)("p-19"))
    niadra.close()
