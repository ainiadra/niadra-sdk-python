"""Typed state in the emulator: the agents' working state (`spec/agent-state.md`, behind `agent_state`), the
objects a claim is verified against, the refresh requests a resolver worker takes and the pushes it makes
(behind `state`).

- `PUT /v1/agent-state` applies `cas` and `merge_by_key` with the spec's rules: 412 `agent_state_conflict` at
  another version, `stored: false, reason: over_cap` past 16 KB. `POST /v1/agent-state/read` reads a state,
  version 0 and an empty body before the first write.
- `observe()` sets an object's field with its status; `POST /v1/state/verify` answers each check from them.
- `request_refresh()` queues a refresh request; `GET /v1/state/refresh-requests` leases the waiting ones for
  60 s, and `POST /v1/objects/push` applies fields by the per-field version rule and settles the requests of
  the objects it pushed.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

CAP = 16 * 1024
DELETE = {"$delete": True}
LEASE = timedelta(seconds=60)


class StateError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass
class _Field:
    value: Any
    status: str = "fresh"
    version: int = 0


@dataclass
class StateStore:
    agent_states: dict[tuple[str, str, str], tuple[dict[str, Any], int, datetime]] = field(
        default_factory=dict
    )
    objects: dict[tuple[str, str, str], dict[str, _Field]] = field(default_factory=dict)
    refreshes: dict[str, dict[str, Any]] = field(default_factory=dict)
    pushes: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # The agents' working state

    def write_agent_state(self, body: dict[str, Any]) -> dict[str, Any]:
        key = (body["scope"]["kind"], body["scope"]["id"], body["agent"])
        with self._lock:
            current, version, _ = self.agent_states.get(key, ({}, 0, datetime.now(timezone.utc)))
            if_version = body.get("if_version")
            if (body["mode"] == "cas" or if_version is not None) and (if_version or 0) != version:
                raise StateError(412, "agent_state_conflict")
            if body["mode"] == "cas":
                new = dict(body["body"])
            else:
                new = dict(current)
                for name, value in body["body"].items():
                    if value == DELETE:
                        new.pop(name, None)
                    else:
                        new[name] = value
            if len(json.dumps(new, sort_keys=True, separators=(",", ":")).encode()) > CAP:
                return {"stored": False, "version": version, "reason": "over_cap"}
            self.agent_states[key] = (new, version + 1, datetime.now(timezone.utc))
            return {"stored": True, "version": version + 1}

    def read_agent_state(self, body: dict[str, Any]) -> dict[str, Any]:
        key = (body["scope"]["kind"], body["scope"]["id"], body["agent"])
        with self._lock:
            found = self.agent_states.get(key)
        if found is None:
            return {"body": {}, "version": 0}
        state, version, at = found
        return {"body": state, "version": version, "updated_at": at.isoformat()}

    # Objects

    def observe(self, ref: str, fields: dict[str, Any], *, status: str = "fresh") -> None:
        """Sets fields of the object `type:namespace:id`, each with its status for a claim."""
        kind, namespace, object_id = ref.split(":", 2)
        with self._lock:
            held = self.objects.setdefault((kind, namespace, object_id), {})
            for name, value in fields.items():
                held[name] = _Field(value, status, held[name].version if name in held else 0)

    def verify(self, body: dict[str, Any]) -> dict[str, Any]:
        verdicts = []
        for check in body["checks"]:
            ref = check["ref"]
            with self._lock:
                held = self.objects.get((ref["type"], ref["namespace"], ref["id"]), {}).get(check["field"])
            status = held.status if held is not None else "expired"
            matches = _same(held.value, check.get("value")) if held is not None else None
            verdicts.append(
                {
                    "ref": ref,
                    "field": check["field"],
                    "status": status,
                    "matches": matches,
                    "claim_safe": status == "fresh" and bool(matches),
                    "declared_gaps": [] if held is not None else ["unobserved"],
                }
            )
        return {"verdicts": verdicts}

    def request_refresh(self, ref: str, *, reason: str = "claim_pending", units: int = 1) -> str:
        kind, namespace, object_id = ref.split(":", 2)
        request_id = f"rr_{uuid4().hex[:12]}"
        with self._lock:
            self.refreshes[request_id] = {
                "request_id": request_id,
                "ref": {"type": kind, "namespace": namespace, "id": object_id},
                "reason": reason,
                "priority": "normal",
                "budget_units": units,
                "lease_until": None,
            }
        return request_id

    def lease_refreshes(self, limit: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        items = []
        with self._lock:
            for request in self.refreshes.values():
                lease = request["lease_until"]
                if lease is not None and lease > now:
                    continue
                request["lease_until"] = now + LEASE
                items.append({**request, "lease_until": request["lease_until"].isoformat()})
                if len(items) >= limit:
                    break
        return {"items": items}

    def push(self, body: dict[str, Any]) -> dict[str, Any]:
        applied = stale = 0
        with self._lock:
            for item in body["objects"]:
                ref = item["ref"]
                key = (ref["type"], ref["namespace"], ref["id"])
                held = self.objects.setdefault(key, {})
                moved = False
                for name, value in item["fields"].items():
                    if name not in held or item["version"] > held[name].version:
                        held[name] = _Field(value, "fresh", item["version"])
                        moved = True
                applied, stale = (applied + 1, stale) if moved else (applied, stale + 1)
                self.pushes.append(item)
                for request_id in [r for r, v in self.refreshes.items() if tuple(v["ref"].values()) == key]:
                    del self.refreshes[request_id]
        return {"applied": applied, "stale_version": stale, "out_of_set": 0}


def _same(a: Any, b: Any) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError):
        return bool(a == b)
