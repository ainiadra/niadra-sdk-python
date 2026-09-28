"""The ways a request reaches Niadra from the harness, each a `path` in the results:

- `edge`: the address the SDK derives from the key, through public DNS and the internet gateway, with
  TLS, the way a customer's agent calls it;
- `vpc`: the same names and the same TLS, but connected to the cell machine's private address inside
  its VPC (`NIADRA_VPC_ADDRESS`), from the benchmark's separate host in the same VPC: the request skips
  the internet gateway and still goes through the cell's ingress, as a customer's agent in the region
  on a private link would;
- `host`: from a harness running on the cell's machine itself (`NIADRA_HOST_ADDRESS`), each request goes
  straight to the deployable that serves its path, over plain HTTP inside the cluster: no DNS, no TLS, no
  ingress. It is how every other system is measured, in the harness's own process or on its own host,
  so it is the path to compare them with (`deploy/cell/host-lines.sh`).

The `vpc` path keeps the public host name in the request (Host header and TLS server name) and only
changes where the connection goes, so the certificate is checked against the real name. `NIADRA_PATHS`
(comma-separated) keeps only the paths it names: on the cell's machine, `host`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

import httpx

TransportFactory = Callable[[], httpx.AsyncBaseTransport]


class PinnedTransport(httpx.AsyncBaseTransport):
    """Connects every request to one address, keeping its host name for the Host header and TLS."""

    def __init__(self, address: str, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.address = address
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host != self.address:
            request.url = request.url.copy_with(host=self.address)
            request.extensions = {**request.extensions, "sni_hostname": host}
            request.headers.setdefault("host", host)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


SERVICES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ingest", ("/v1/batch", "/v1/ingest", "/v1/otel", "/v1/media", "/v1/feedback")),
    ("read", ("/v1/context", "/v1/history", "/v1/objects", "/v1/subject-tokens", "/mcp")),
    ("insights", ("/v1/insights", "/mcp/insights")),
)
"""The cell's deployables and the paths each serves, as its ingress routes them (niadra-infra, `routes` in
`k8s/charts/niadra/values.yaml`); the longest prefix wins, and every other path goes to `admin`."""


def service_of(path: str) -> str:
    """The deployable that serves `path`: a prefix matches whole path segments, as a `Prefix` ingress rule."""
    best, length = "admin", 0
    for service, prefixes in SERVICES:
        for prefix in prefixes:
            if (path == prefix or path.startswith(prefix + "/")) and len(prefix) > length:
                best, length = service, len(prefix)
    return best


class ServiceTransport(httpx.AsyncBaseTransport):
    """Sends each request to the deployable that serves its path, inside the cell. `address` is a base URL,
    `http://read.niadra.svc.cluster.local:8000`, or a template with `{service}` for one per deployable."""

    def __init__(self, address: str, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.address = address
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = httpx.URL(self.address.format(service=service_of(request.url.path)))
        request.url = request.url.copy_with(scheme=target.scheme, host=target.host, port=target.port)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass(frozen=True)
class Route:
    """One path to Niadra: its name in the results, the base URL, and how to connect."""

    path: str
    base: str
    transport: TransportFactory | None = None


def niadra_routes(
    edge: str | None,
    *,
    edge_transport: TransportFactory | None = None,
    env: dict[str, str] | None = None,
) -> list[Route]:
    """`edge` (the SDK's public address), `vpc` when `NIADRA_VPC_ADDRESS` is set and `host` when
    `NIADRA_HOST_ADDRESS` is, all of them or those `NIADRA_PATHS` names. `edge_transport` stands in for the
    network in dry runs."""
    source = os.environ if env is None else env
    routes: list[Route] = []
    if edge:
        routes.append(Route("edge", edge, edge_transport))
        if address := source.get("NIADRA_VPC_ADDRESS"):
            routes.append(Route("vpc", edge, lambda: PinnedTransport(address)))
        if host := source.get("NIADRA_HOST_ADDRESS"):
            routes.append(Route("host", edge, lambda: ServiceTransport(host)))
    if wanted := {p.strip() for p in source.get("NIADRA_PATHS", "").split(",") if p.strip()}:
        routes = [r for r in routes if r.path in wanted]
    return routes
