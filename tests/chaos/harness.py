"""What the chaos test drives: the proxy process, the Niadra behind it, and the proxy's log read as
deliveries.

Without a cell, the Niadra behind the proxy is `niadra-mock` served over HTTP in this process. With
`NIADRA_CHAOS_API`, `NIADRA_CHAOS_KEY` and `NIADRA_CHAOS_SUBJECT` set, it is that cell, and the subject must
have memory there already (a pack that is not empty).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import httpx

from niadra.coordination.destination import canonical_destination, suppression_key
from niadra.models.common import Handle
from niadra_mock import MOCK_KEY, MockApp

ROOT = Path(__file__).resolve().parents[2]
EARLIER = [
    ("customer", "Meu pedido 4471 chegou com a tampa quebrada."),
    ("ai_agent", "Abri a troca do 4471."),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Proxy:
    """`tests/chaos/proxy.py` as a child process, in front of `upstream`."""

    def __init__(self, upstream: str, log: Path) -> None:
        self.upstream = upstream
        self.log = log
        self.port = free_port()
        self.control = free_port()
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        ports = ["--port", str(self.port), "--control", str(self.control)]
        self._process = subprocess.Popen(  # noqa: S603 - this interpreter, this repository's proxy
            [
                sys.executable,
                "-m",
                "tests.chaos.proxy",
                *ports,
                "--upstream",
                self.upstream,
                "--log",
                str(self.log),
            ],
            cwd=ROOT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    pass
                with socket.create_connection(("127.0.0.1", self.control), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("the chaos proxy did not start")

    def kill(self) -> None:
        """SIGKILL: open connections are reset and new ones refused."""
        if self._process is not None:
            self._process.send_signal(signal.SIGKILL)
            self._process.wait()
            self._process = None

    def mode(self, mode: str, delay: float = 0.0) -> None:
        body = {"mode": mode, "delay": delay}
        httpx.post(f"http://127.0.0.1:{self.control}/mode", json=body).raise_for_status()

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process.wait()
            self._process = None


@dataclass
class Ledger:
    """The proxy's log since `since`: what reached Niadra, and what Niadra took."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def read(cls, log: Path, since: float = 0.0) -> Ledger:
        if not log.exists():
            return cls()
        lines = log.read_text(encoding="utf-8").splitlines()
        return cls([e for e in (json.loads(line) for line in lines if line) if e["t"] >= since])

    def taken(self, path: str) -> list[dict[str, Any]]:
        """The requests to `path` that reached Niadra and were answered 2xx."""
        return [e for e in self.entries if e["path"] == path and e.get("forwarded") and _ok(e)]

    def turns_accepted(self) -> int:
        return sum(e.get("accepted", 0) for e in self.taken("/v1/turns"))

    def turn_ids(self) -> set[str]:
        return {t for e in self.taken("/v1/turns") for t in e.get("turns", [])}

    def events_accepted(self) -> int:
        """Events Niadra stored: accepted items, less the heartbeats among them."""
        return sum(e.get("accepted", 0) - e.get("heartbeats", 0) for e in self.taken("/v1/batch"))

    def event_keys(self) -> Counter[str]:
        """Each event key, by the number of 2xx requests that carried it."""
        return Counter(k for e in self.taken("/v1/batch") for k in e.get("items", []))

    def declaration_keys(self) -> set[str]:
        return {e["key"] for e in self.taken("/v1/coordination/declare")}

    def duplicates(self) -> int:
        """Writes Niadra saw again and did not store twice (a resend after an answer the SDK never got)."""
        return sum(e.get("duplicates", 0) for e in self.taken("/v1/turns") + self.taken("/v1/batch"))

    def last(self, predicate: Any) -> float | None:
        times = [e["t"] for e in self.entries if predicate(e)]
        return max(times) if times else None


def _ok(entry: dict[str, Any]) -> bool:
    return 200 <= entry.get("status", 0) < 300


class _Threaded(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _Quiet(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


class MockUpstream:
    """`niadra-mock` over HTTP, with the turns and coordination features on."""

    def __init__(self) -> None:
        self.app = MockApp()
        self.app.cell.features.update({"turns", "coordination"})
        self.key = MOCK_KEY
        self.subject = Handle(type="phone_e164", value="+5511900007001")
        self._server = make_server("127.0.0.1", free_port(), self.app.wsgi, _Threaded, _Quiet)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def prepare(self, purposes: tuple[str, ...]) -> None:
        """Earlier memory of the subject, and their opt-out of `purposes`, both before the conversation."""
        items = [
            {
                "type": "event",
                "kind": "message",
                "idempotency_key": f"earlier-{n}",
                "channel": "whatsapp",
                "conversation_id": "earlier",
                "handles": [self.subject.model_dump()],
                "speaker": {"role": role},
                "direction": "inbound" if role == "customer" else "outbound",
                "content": {"type": "text", "text": text},
            }
            for n, (role, text) in enumerate(EARLIER)
        ]
        answer = httpx.post(f"{self.url}/v1/batch", json={"items": items}, headers=_bearer(self.key))
        answer.raise_for_status()
        for purpose in purposes:
            self.app.cell.agent_features.suppress(self.subject, purpose)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class CellUpstream:
    """A running cell: the opt-out is declared by the same source before the conversation, and read back."""

    def __init__(self) -> None:
        self.url = os.environ["NIADRA_CHAOS_API"].rstrip("/")
        self.key = os.environ["NIADRA_CHAOS_KEY"]
        self.subject = Handle(type="phone_e164", value=os.environ["NIADRA_CHAOS_SUBJECT"])

    def prepare(self, purposes: tuple[str, ...]) -> None:
        with httpx.Client(base_url=self.url, headers=_bearer(self.key), timeout=30) as api:
            for purpose in purposes:
                body = {
                    "kind": "suppression.added",
                    "agent": "chaos-setup",
                    "subject": self.subject.model_dump(),
                    "detail": {"purpose": purpose, "reason": "opt_out"},
                }
                headers = {"Idempotency-Key": f"chaos-{uuid.uuid4().hex}"}
                api.post("/v1/coordination/declare", json=body, headers=headers).raise_for_status()
            salt = api.get("/v1/suppressions/salt").raise_for_status().json()["salt"]
            key = suppression_key(salt, canonical_destination(str(self.subject.type), self.subject.value))
            deadline = time.monotonic() + 60
            while True:
                listed = _suppressions(api)
                if all(any(e["key"] == key and e["purpose"] == p for e in listed) for p in purposes):
                    return
                if time.monotonic() > deadline:
                    raise AssertionError("the opt-out did not reach the suppression list")
                time.sleep(0.5)

    def close(self) -> None:
        pass


def _suppressions(api: httpx.Client) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = api.get("/v1/suppressions", params={"limit": 200, **({"cursor": cursor} if cursor else {})})
        body = page.raise_for_status().json()
        items = [i for i in items if i["id"] not in {r["id"] for r in body["items"]}] + body["items"]
        cursor = body.get("next_cursor")
        if not cursor:
            return [i for i in items if not i.get("removed")]


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def upstream() -> MockUpstream | CellUpstream:
    return CellUpstream() if os.environ.get("NIADRA_CHAOS_API") else MockUpstream()
