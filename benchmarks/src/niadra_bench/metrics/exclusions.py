"""What Niadra's pack left out of each probe, and why: the exclusion manifest (estudo 17, WP-11).

Every context read writes a receipt whose manifest lists what the pack deliberately left out, each item
with its reason (`policy`, `verification`, `quarantine`, `budget`) and the rule that applied (for the
budget: `skeleton`, `view`, `duplicate`, `low_value`, `budget`; `context_use` for an unused unit). After
the accuracy pass the harness reads, for each Niadra probe, its conversation's read receipt
(`POST /v1/receipts/search`) and that receipt's lineage (`GET /v1/lineage/receipt/{id}`, the manifest's
`excluded`), and keeps it in the probe's row as `meta.exclusions`: the items (ids, kinds, categories,
reasons and rules; never a value) and a count per reason and rule.

With it, a lost answer is attributed by data instead of inference: `withheld` when the pack held back at
least one item for policy, verification or quarantine (the memory had something the rules did not give
at the probe's level), `not_withheld` when the manifest held nothing back (the memory did not find it,
or found it and the budget or the view left it out; the `budget` count says which), `unknown` when no
manifest was read. The governance reads need an admin credential: the local cell's `admin_key`, or the
sandbox's admin person in the region (`ControlPlane`); without one the rows carry no manifest.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

import httpx

#: Reasons that hold an item back from a caller who could see it after more verification or another
#: purpose; `budget` is the pack's size and view, not a rule about the caller.
WITHHELD = frozenset({"policy", "verification", "quarantine"})
_KEEP = ("id", "kind", "category", "reason", "rule", "predicate", "object_id")


class GovernanceUnavailableError(RuntimeError):
    """The deployment does not serve the governance reads to this credential."""


def compact(receipt_id: str, excluded: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = [{k: item[k] for k in _KEEP if item.get(k) is not None} for item in excluded]
    by_reason = Counter(
        f"{item.get('reason')}/{item['rule']}" if item.get("rule") else str(item.get("reason"))
        for item in items
    )
    return {
        "receipt_id": receipt_id,
        "items": items,
        "by_reason": dict(sorted(by_reason.items())),
        "withheld": sum(1 for item in items if item.get("reason") in WITHHELD),
    }


async def fetch(
    http: httpx.AsyncClient, base_url: str, headers: dict[str, str], conversation: str
) -> dict[str, Any] | None:
    """The exclusions of the conversation's context read, or None while its receipt is not written yet
    (in the region receipts reach the chain through a queue)."""
    base = base_url.rstrip("/")
    body = {"conversation_id": conversation, "kind": "read", "limit": 20}
    found = await http.post(f"{base}/v1/receipts/search", json=body, headers=headers)
    if found.status_code in (401, 403, 404, 405):
        raise GovernanceUnavailableError(f"receipts search answered {found.status_code}")
    found.raise_for_status()
    receipts = [r for r in found.json().get("items", []) if r.get("manifest_hash")]
    if not receipts:
        return None
    receipt = max(receipts, key=lambda r: str(r.get("read_at") or ""))
    lineage = await http.get(f"{base}/v1/lineage/receipt/{receipt['receipt_id']}", headers=headers)
    if lineage.status_code in (401, 403, 404, 405):
        raise GovernanceUnavailableError(f"receipt lineage answered {lineage.status_code}")
    lineage.raise_for_status()
    data = lineage.json()
    if not data.get("manifest_found", True):
        return None
    return compact(str(receipt["receipt_id"]), data.get("excluded") or [])


def _verdict(row: Any) -> bool | None:
    verdict: bool | None = row.judge if row.judge is not None else row.deterministic
    return verdict


def attribute(rows: Iterable[Any], valid: set[str]) -> dict[str, Any]:
    """Niadra's lost answers on the valid cases, by cause and by category: `withheld`, `not_withheld`
    or `unknown` (the module docstring)."""
    total: Counter[str] = Counter()
    by_category: dict[str, Counter[str]] = {}
    recorded = answered = 0
    for row in rows:
        if row.system != "niadra" or row.purpose != "answer" or row.case_id not in valid:
            continue
        answered += 1
        manifest = (row.meta or {}).get("exclusions")
        recorded += manifest is not None
        if _verdict(row) is not False:
            continue
        cause = "unknown" if manifest is None else "withheld" if manifest["withheld"] else "not_withheld"
        total[cause] += 1
        by_category.setdefault(row.category, Counter())[cause] += 1
    return {
        "answers": answered,
        "with_manifest": recorded,
        "lost": dict(total),
        "lost_by_category": {c: dict(v) for c, v in sorted(by_category.items())},
    }
