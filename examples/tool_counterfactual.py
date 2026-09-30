"""The tool counterfactual: does an element of the constraints block change what a tool returns?

For each recorded call of the tool, the runner runs the recorded arguments twice (the tool's own noise) and
once without the element (here the hard constraints), all at the same moment and only when the tool is safe
to run again (`dry_run=True`, or its binding's `dry_run_param`). Niadra gets positions and overlaps only,
never the items. From the company's CI:

    niadra counterfactual --tools examples.tool_counterfactual:TOOLS --tool search_products \\
        --element hard --turn <turn id> --turn <turn id> --label "$GIT_SHA"

Needs the space's `turns`, `signals` and `measurement` features, a key with the `replay` scope, and the tool
bound in the space's `tool-bindings` document, which the SDK profile serves: how its arguments carry a
constraint and how its result shows the items, for example

    {"tool": "search_products",
     "args": [{"attr": "item_variant.color", "param": "color", "negation": {"param": "not_color"}}],
     "results": [{"path": "cards[*]", "type": "item_variant", "namespace": "store", "id": "variant_id",
                  "fields": {"color": "color"}}]}
"""

from __future__ import annotations

import sys
from typing import Any

from niadra import Niadra
from niadra.replay import Counterfactual

CATALOG = [
    {"variant_id": str(n), "color": color, "price": price}
    for n, (color, price) in enumerate(
        [("red", 120), ("blue", 90), ("red", 80), ("black", 150), ("blue", 60)]
    )
]


@Niadra.tool("search_products", dry_run=True)
def search_products(not_color: list[str] | None = None, color: str | None = None) -> dict[str, Any]:
    """A read-only search, cheapest first: safe to run again."""
    refused = set(not_color or ())
    cards = [c for c in CATALOG if c["color"] not in refused and (color is None or c["color"] == color)]
    return {"cards": sorted(cards, key=lambda c: c["price"])}


TOOLS = {"search_products": search_products}


def measure(niadra: Niadra, turn_ids: list[str], label: str | None = None) -> dict[str, Any]:
    """Runs the counterfactual of the hard constraints over these turns and returns the report."""
    run = Counterfactual(niadra, TOOLS).run(turn_ids, tool="search_products", element="hard", label=label)
    return {"cases": len(run.cases), "untouched": run.untouched, **(run.report or {})}


if __name__ == "__main__":
    niadra = Niadra()
    print(measure(niadra, sys.argv[1:]))
    niadra.close()
