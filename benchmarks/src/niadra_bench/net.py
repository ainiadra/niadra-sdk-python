"""The ways a request reaches Niadra from the harness, each a `path` in the results:

- `edge`: the address the SDK derives from the key, through public DNS and the internet gateway, with
  TLS, the way a customer's agent calls it;
- `vpc`: the same names and the same TLS, but connected to the cell machine's private address inside
  its VPC (`NIADRA_VPC_ADDRESS`), from the benchmark's separate host in the same VPC: the request skips
  the internet gateway and still goes through the cell's ingress, as a customer's agent in the region
  on a private link would;
- `cluster`: a service's own address inside the cluster (`NIADRA_CLUSTER_URL`,
  `NIADRA_CLUSTER_INGEST_URL`), for a harness that runs as a pod of the cell. Runs from the separate
  host (since 26/09/2026) do not set it.

The `vpc` path keeps the public host name in the request (Host header and TLS server name) and only
changes where the connection goes, so the certificate is checked against the real name.
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


@dataclass(frozen=True)
class Route:
    """One path to Niadra: its name in the results, the base URL, and how to connect."""

    path: str
    base: str
    transport: TransportFactory | None = None


def niadra_routes(
    edge: str | None,
    cluster_env: str,
    *,
    edge_transport: TransportFactory | None = None,
    env: dict[str, str] | None = None,
) -> list[Route]:
    """`edge` (the SDK's public address), `vpc` when `NIADRA_VPC_ADDRESS` is set, and `cluster` when
    `cluster_env` names a service's address. `edge_transport` stands in for the network in dry runs."""
    source = os.environ if env is None else env
    routes: list[Route] = []
    if edge:
        routes.append(Route("edge", edge, edge_transport))
        if address := source.get("NIADRA_VPC_ADDRESS"):
            routes.append(Route("vpc", edge, lambda: PinnedTransport(address)))
    if cluster := source.get(cluster_env):
        routes.append(Route("cluster", cluster, edge_transport))
    return routes
