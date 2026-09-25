"""The benchmark's billing agent set up the way Niadra's docs tell a customer to (guides/internal-agents).

An internal agent's source declares `trusted_action_ops`, the closed list of operations it may record. The
sandbox's starter billing source declares `credit` only, so it refused the refunds, reimbursements and
redeliveries the dataset's billing agent records. A company whose agent does those declares them when it
creates the source. With the sandbox's admin account (the bootstrap document) and the control plane's
address, the harness does the same: one source for the dataset's operations, reused across runs, and a new
key per run, revoked when the run ends.

The same account sets Niadra's `memory_v2` space flag when a run asks for it (`--niadra-memory-v2`): a
configuration diff on the space's document, approved by the same person (the sandbox has no second
admin, so the four-eyes rule lets the author approve), and put back when the run ends. The flag's
document and field are `NIADRA_MEMORY_V2_FLAG` (`<document type>/<dotted field>`, default
`settings/memory_v2`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import struct
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from niadra_bench.dataset.model import Case

SOURCE_PREFIX = "bench-billing"
KEY_WAIT_S = 180.0
MEMORY_V2_FLAG = "settings/memory_v2"


def dataset_operations(cases: Iterable[Case]) -> list[str]:
    """The operations the dataset's billing agent records, which its source must declare."""
    return sorted(
        {
            s.record.operation
            for case in cases
            for s in case.sessions
            if s.record is not None and s.record.kind == "action" and s.record.operation
        }
    )


def source_name(operations: Iterable[str]) -> str:
    """One source per list of operations: a run with the same dataset finds and reuses it."""
    digest = hashlib.sha256(",".join(sorted(operations)).encode()).hexdigest()[:8]
    return f"{SOURCE_PREFIX}-{digest}"


def totp(secret_b32: str, at: float | None = None) -> str:
    """RFC 6238, SHA-1, 6 digits, 30 s, as the sandbox's end-to-end check computes it."""
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = struct.pack(">Q", int((time.time() if at is None else at) // 30))
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


@dataclass(frozen=True)
class IssuedKey:
    source_id: str
    key_id: str
    secret: str


class ControlPlane:
    """The few control API calls a person makes in the Console to add an agent's source and its key."""

    def __init__(self, url: str, document: dict[str, Any], http: httpx.AsyncClient) -> None:
        self.url = url.rstrip("/")
        self.document = document
        self.http = http
        self._person: dict[str, str] | None = None

    @classmethod
    def available(cls, document: dict[str, Any] | None, env: dict[str, str] | None = None) -> str | None:
        """The control plane's address when the run can act as the sandbox's admin, else None."""
        source = os.environ if env is None else env
        url = source.get("NIADRA_CONTROL_URL")
        needed = ("admin_email", "password", "space_id")
        if not url or not document or not all(document.get(k) for k in needed):
            return None
        return url

    async def _login(self) -> dict[str, str]:
        body: dict[str, Any] = {
            "email": self.document["admin_email"],
            "password": self.document["password"],
            "space_id": self.document["space_id"],
        }
        for attempt in range(3):
            if self.document.get("totp_secret"):
                body["totp"] = totp(self.document["totp_secret"])
            response = await self.http.post(f"{self.url}/v1/auth/login", json=body)
            if response.status_code == 401 and "totp" in body and attempt < 2:
                # The control plane takes each code once; another login may have used this step's.
                await asyncio.sleep(30 - time.time() % 30 + 0.5)
                continue
            _expect(response, 200)
            return {"authorization": f"Bearer {response.json()['access_token']}"}
        raise RuntimeError("could not log in to the control plane")

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """A call as the admin; a token that expired during a long run is renewed once."""
        if self._person is None:
            self._person = await self._login()
        response = await self.http.request(method, f"{self.url}{path}", headers=self._person, **kwargs)
        if response.status_code == 401:
            self._person = await self._login()
            response = await self.http.request(method, f"{self.url}{path}", headers=self._person, **kwargs)
        return response

    async def billing_key(self, operations: list[str]) -> IssuedKey:
        space_id = self.document["space_id"]
        name = source_name(operations)
        listed = _expect(await self._call("GET", "/v1/sources", params={"space_id": space_id}), 200)
        found = next(
            (
                s
                for s in listed
                if s["name"] == name
                and s.get("revoked_at") is None
                and set(operations) <= set(s.get("trusted_action_ops") or [])
            ),
            None,
        )
        if found is None:
            body = {
                "space_id": space_id,
                "name": name,
                "audience": "internal_agent",
                "channel": "erp",
                "purposes": ["billing"],
                "trusted_action_ops": operations,
            }
            found = _expect(await self._call("POST", "/v1/sources", json=body), 201)
        issued = _expect(await self._call("POST", f"/v1/sources/{found['source_id']}/keys", json={}), 201)
        return IssuedKey(str(found["source_id"]), str(issued["key"]["key_id"]), str(issued["secret"]))

    async def set_flag(self, flag: str, value: bool | None) -> bool | None:
        """Sets a space flag (`<document type>/<dotted field>`), or removes it with None, and returns the
        value it had (None when the document did not set it). No diff when nothing changes."""
        doc_type, _, path = flag.partition("/")
        if not doc_type or not path:
            raise ValueError(f"a flag is <document type>/<dotted field>, not {flag!r}")
        space_id = self.document["space_id"]
        current = _expect(
            await self._call("GET", f"/v1/config/{doc_type}", params={"space_id": space_id}), 200
        )
        document = current.get("document")
        document = dict(document) if isinstance(document, dict) else {}
        parts = path.split(".")
        node = document
        for part in parts[:-1]:
            child = node.get(part)
            node[part] = dict(child) if isinstance(child, dict) else {}
            node = node[part]
        previous = node.get(parts[-1])
        if previous is value:
            return value
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
        reason = f"benchmark run: {path} {'default' if value is None else 'on' if value else 'off'}"
        body = {"space_id": space_id, "type": doc_type, "document": document, "reason": reason}
        diff = _expect(await self._call("POST", "/v1/config/diffs", json=body), 201)
        if diff.get("status") != "applied":
            diff = _expect(await self._call("POST", f"/v1/config/diffs/{diff['diff_id']}/approve"), 200)
        if diff.get("status") != "applied":
            raise RuntimeError(f"the {flag} diff was not applied: {diff.get('status')}")
        return previous if isinstance(previous, bool) else None

    async def revoke(self, key: IssuedKey) -> None:
        path = f"/v1/sources/{key.source_id}/keys/{key.key_id}/revoke"
        _expect(await self._call("POST", path, json={"reason": "benchmark run finished"}), 200)


async def wait_until_served(
    http: httpx.AsyncClient, base_url: str, key: str, timeout_s: float = KEY_WAIT_S
) -> None:
    """A new key reaches the cell with the control plane's next snapshot: 401 until then, 503 while a
    space database is being created (the same wait as the sandbox tenant job)."""
    deadline = time.monotonic() + timeout_s
    subject = {"type": "system_id", "scope": "crm", "value": "bench-key-check"}
    headers = {"authorization": f"Bearer {key}"}
    while True:
        response = await http.post(f"{base_url}/v1/context", json={"subject": subject}, headers=headers)
        if response.status_code not in (401, 503):
            _expect(response, 200)
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"the new billing key was not served after {timeout_s:.0f} s")
        await asyncio.sleep(5)


def _expect(response: httpx.Response, status: int) -> Any:
    if response.status_code != status:
        request = response.request
        raise RuntimeError(
            f"{request.method} {request.url.path} -> {response.status_code}: {response.text[:300]}"
        )
    return response.json()
