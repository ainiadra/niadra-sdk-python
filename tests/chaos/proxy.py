"""An HTTP proxy in front of Niadra that fails on command, for the chaos test (`tests/test_chaos.py`).

    python -m tests.chaos.proxy --port 8801 --control 8802 --upstream http://127.0.0.1:8800 --log log.jsonl

It runs as its own process, so the test can kill it with SIGKILL: the connections the SDK holds are reset and
new ones are refused, as when Niadra's process dies. `POST /mode` on the control port sets the other failures:

- `up`: every request goes to the upstream and back;
- `blackhole`: a request is read and never answered, until the mode changes and the connection is dropped;
- `503`: every request is answered 503 with `Retry-After: 1`, and nothing reaches the upstream;
- `slow`: every request reaches the upstream at once, and its answer is held `delay` seconds.

Each request is logged as a JSON line: whether it reached the upstream, its status, its idempotency key, and
for the writes, the turn ids and event keys it carried with the upstream's `accepted` and `duplicates` counts.
The log is what the test counts deliveries and duplicates by, from outside the SDK.
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "host", "proxy-connection"}


class State:
    def __init__(self, upstream: str, log_path: str) -> None:
        parts = urlsplit(upstream)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self.mode = "up"
        self.delay = 0.0
        self.changed = threading.Condition()
        self._log = open(log_path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115 - lives with the process
        self._log_lock = threading.Lock()

    def set(self, mode: str, delay: float) -> None:
        with self.changed:
            self.mode, self.delay = mode, delay
            self.changed.notify_all()

    def wait_while(self, mode: str) -> None:
        with self.changed:
            while self.mode == mode:
                self.changed.wait()

    def log(self, entry: dict[str, Any]) -> None:
        with self._log_lock:
            self._log.write(json.dumps(entry) + "\n")


def _items(path: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    """The turn ids or the event keys a write carries."""
    if path not in ("/v1/turns", "/v1/batch") or not body:
        return {}
    try:
        raw = gzip.decompress(body) if headers.get("content-encoding") == "gzip" else body
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    if path == "/v1/turns":
        return {"turns": [t.get("turn_id") for t in data.get("turns", [])]}
    items = data.get("items", [])
    return {
        "items": [i.get("idempotency_key") for i in items if i.get("type", "event") != "heartbeat"],
        "heartbeats": sum(1 for i in items if i.get("type") == "heartbeat"),
    }


def _counts(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in ("accepted", "duplicates") if k in data}


def handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _relay(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            path = urlsplit(self.path).path
            mode, delay = state.mode, state.delay
            entry: dict[str, Any] = {
                "t": time.time(),
                "mode": mode,
                "method": self.command,
                "path": path,
                "key": headers.get("idempotency-key"),
                **_items(path, headers, body),
            }
            if mode == "blackhole":
                state.log({**entry, "forwarded": False})
                state.wait_while("blackhole")
                self.close_connection = True
                return
            if mode == "503":
                state.log({**entry, "forwarded": False, "status": 503})
                answer = b'{"title":"unavailable","status":503,"code":"unavailable"}'
                problem = [("Content-Type", "application/problem+json"), ("Retry-After", "1")]
                self._answer(503, problem, answer)
                return
            upstream = http.client.HTTPConnection(state.host, state.port, timeout=60)
            try:
                forward = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
                upstream.request(self.command, self.path, body=body or None, headers=forward)
                response = upstream.getresponse()
                answer = response.read()
                answer_headers = [(k, v) for k, v in response.getheaders() if k.lower() not in _HOP]
                status = response.status
            finally:
                upstream.close()
            state.log({**entry, "forwarded": True, "status": status, **_counts(answer)})
            if mode == "slow":
                time.sleep(delay)
            self._answer(status, answer_headers, answer)

        def _answer(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
            try:
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.close_connection = True  # the client gave up first

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _relay  # noqa: N815 - the base class's names

    return Proxy


def control(state: State) -> type[BaseHTTPRequestHandler]:
    class Control(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            state.set(body["mode"], float(body.get("delay", 0)))
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:
            answer = json.dumps({"mode": state.mode}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

    return Control


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client that reset its connection: what the outage is made of


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--control", type=int, required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    state = State(args.upstream, args.log)
    admin = _Server(("127.0.0.1", args.control), control(state))
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    _Server(("127.0.0.1", args.port), handler(state)).serve_forever()


if __name__ == "__main__":
    main()
