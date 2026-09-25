"""`niadra-mock`: serves the emulator over HTTP on localhost."""

from __future__ import annotations

import argparse
import logging
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from niadra_mock import MOCK_KEY
from niadra_mock.app import MockApp


class _ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:  # noqa: ARG002
        # Method, path and status only; bodies carry personal data and never reach the log.
        logging.getLogger("niadra_mock").info("%s %s", self.requestline.split(" HTTP/")[0], code)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="niadra-mock", description="A local, in-memory Niadra API.")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765)")
    parser.add_argument(
        "--memory-v2",
        action="store_true",
        help="answer as a space with memory v2: a read's query picks this turn's slots",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    app = MockApp()
    app.cell.enable_memory_v2(args.memory_v2)
    server = make_server(
        args.host, args.port, app.wsgi, server_class=_ThreadingServer, handler_class=_QuietHandler
    )
    print(f"niadra-mock listening on http://{args.host}:{args.port}")
    print(f"  export NIADRA_BASE_URL=http://{args.host}:{args.port}")
    print(f"  export NIADRA_API_KEY={MOCK_KEY}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
