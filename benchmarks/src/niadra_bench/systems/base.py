"""A memory system the harness talks to over HTTP, added by one adapter module in this package.

An adapter says how the system is used, the way its own documentation shows:

- `seed_calls`: the calls that write one customer's history, in order;
- `read_call` and `memories`: the call a turn makes before the model, and the lines of its answer the
  agent receives;
- `exchange_call`: the call that writes one live exchange (the customer's message and the agent's
  answer, or the customer's message alone);
- `open_call` (optional): the call that reads one item a search returned.

Everything else is the same for every system and lives here: seeding with the run's concurrency, the
accuracy pass, metric 1 (the read, open loop), metric 6 (write an exchange, then read until its value
shows), metric 7 (the read behind the fault proxy with a plain HTTP client at its defaults and
`raise_for_status()`, as Mem0's REST server is called), metric 8 (the read as `search`, `open_call` as
`open`) and metric 9 (`exchange_call`, open loop).

To add a system: one module in this package with one `HttpSystem` subclass, and its container in
`deploy/compose/systems.yaml`. The registry (`niadra_bench.systems`) finds the class by its `system`
key, `bench run --systems` accepts that key, and results name it the same way.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import random
import time
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from niadra_bench.dataset.model import Case
from niadra_bench.identity import Identities
from niadra_bench.metrics import latency
from niadra_bench.metrics.operations import EMPTY, OK, Operation, timed
from niadra_bench.targets.base import Retrieved, Stopwatch, Target
from niadra_bench.text import contains

#: Where a system runs, seen from the harness: its container on the same host.
HOST_PATH = latency.HOST
WRITE_ATTEMPTS = 60
READY_TIMEOUT_S = 180.0

# One live exchange, as metrics 6 and 9 write it (the same text Niadra and Mem0 receive).
FRESH_MESSAGES = {
    "pt": "Oi, o número do meu pedido novo é {code}, pode anotar?",
    "en": "Hi, my new order number is {code}, can you note it?",
}
FRESH_QUERIES = {"pt": "número do pedido novo", "en": "new order number"}


@dataclass(frozen=True)
class Call:
    """One HTTP request, relative to the system's base URL."""

    method: str
    path: str
    json: Any = None
    params: dict[str, str] | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def send(
        self, client: httpx.AsyncClient, base: str, headers: dict[str, str]
    ) -> Awaitable[httpx.Response]:
        return client.request(
            self.method,
            f"{base}{self.path}",
            json=self.json,
            params=self.params,
            headers={**headers, **self.headers},
        )


class HttpSystem(Target):
    """A memory system behind an HTTP API, measured through the adapter's calls."""

    #: The key results use, e.g. `ai_memory`.
    system: str
    #: The system's own name, for logs and the README.
    title: ClassVar[str]
    #: Its directory under deploy/systems/, whose compose.yaml runs it (several keys may share one).
    compose: ClassVar[str]
    #: The environment variable with the server's base URL, and the address in the compose files.
    url_env: ClassVar[str]
    default_url: ClassVar[str]
    #: The environment variable with the bearer token, when the server takes one.
    token_env: ClassVar[str | None] = None
    #: The release the container entry pins (deploy/compose/systems.yaml), recorded in the results.
    version: ClassVar[str]
    #: The line before the memory lines, when the system's documentation shows one.
    header: ClassVar[str] = ""
    #: Whether the system has a call that reads one item a search returned (`open_call`).
    has_open: ClassVar[bool] = False
    #: The system's model gateway (`bench serve llm-meter`), when it calls a model: an environment
    #: variable and its default. Its counters before seeding and after settling are the model spend.
    meter_env: ClassVar[str | None] = None
    meter_default: ClassVar[str | None] = None
    #: One store per customer: the application knows who the customer is (Mem0's best case).
    scenario = "known_id"

    def __init__(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = 8,
        settle_timeout_s: float = 1200.0,
    ) -> None:
        self.url = (url or os.environ.get(self.url_env) or self.default_url).rstrip("/")
        self.token = (
            token if token is not None else (os.environ.get(self.token_env, "") if self.token_env else "")
        )
        self.transport = transport
        self._http: httpx.AsyncClient | None = None
        self._limit = asyncio.Semaphore(concurrency)
        self.settle_timeout_s = settle_timeout_s
        self.counters: dict[str, int] = {"writes": 0, "write_retries": 0, "reads": 0}

    # What an adapter provides

    @abstractmethod
    def seed_calls(self, case: Case, ids: Identities) -> list[Call]:
        """Every call that writes the case's history, in chronological order."""

    @abstractmethod
    def read_call(self, case: Case, ids: Identities, question: str) -> Call:
        """The call a turn makes before the model, for this customer and question."""

    @abstractmethod
    def memories(self, response: httpx.Response) -> list[str]:
        """The memory lines of a read's answer, as the agent receives them."""

    @abstractmethod
    def exchange_call(
        self, case: Case, ids: Identities, conversation: str, customer: str, agent: str | None
    ) -> Call:
        """The call that writes one live exchange of this customer, in `conversation`."""

    def open_call(self, case: Case, ids: Identities, read: httpx.Response) -> Call | None:  # noqa: ARG002
        """The call that reads one item the read returned, or None when the system has no such call."""
        return None

    def ensure_calls(self, case: Case, ids: Identities) -> list[Call]:  # noqa: ARG002
        """The calls that must come before a customer's first write (a system that wants its user created
        first); written, untimed, before seeding and before a freshness trial's new customer."""
        return []

    def health_call(self) -> Call | None:
        """A call that answers 200 once the server is up; None to skip the wait."""
        return None

    def retry_of(self, call: Call, response: httpx.Response) -> Call | None:  # noqa: ARG002
        """What is left to send after a write's answer (a server that takes part of a batch returns the
        rest); raises on an answer that is a failure. The default takes any 2xx as complete."""
        response.raise_for_status()
        return None

    def cost_row(self) -> dict[str, Any] | None:
        """The system's cost line (metric 3) when it is not measured through a model gateway (a system
        that calls no model), without the agent's prompt cost; None: measured, or not priced."""
        return None

    @property
    def meter_url(self) -> str | None:
        if not self.meter_env:
            return None
        return os.environ.get(self.meter_env) or self.meter_default

    async def meter_snapshot(self) -> dict[str, Any] | None:
        """The gateway's counters, or None when the system has no gateway (or it does not answer)."""
        if not self.meter_url:
            return None
        try:
            response = await self.http.get(f"{self.meter_url.rstrip('/')}/_meter", timeout=10)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return data

    # The harness side, the same for every system

    @property
    def headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"} if self.token else {}

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(f"{self.system} is not started")
        return self._http

    def customer(self, ids: Identities) -> str:
        """The customer's one identifier across channels (the id a Mem0 application sends in `known_id`)."""
        return ids.mem0_user("known_id", "whatsapp")

    def store(self, ids: Identities) -> str:
        """The customer's store in the system (a user, group, bank, project or tag), as the system names it:
        the customer's id unless the system needs another form."""
        return self.customer(ids)

    async def start(self) -> None:
        self._http = httpx.AsyncClient(transport=self.transport, timeout=60.0)
        call = self.health_call()
        if call is None:
            return
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            try:
                response = await call.send(self.http, self.url, self.headers)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"{self.title} at {self.url} did not answer in {READY_TIMEOUT_S:.0f} s")
            await asyncio.sleep(2)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def versions(self) -> dict[str, str]:
        return {self.system: self.version}

    def seed_report(self) -> dict[str, Any]:
        counters, self.counters = self.counters, dict.fromkeys(self.counters, 0)
        return {"writes": counters["writes"], "write_retries": counters["write_retries"]}

    async def write(self, call: Call) -> None:
        """Sends a write until the server has taken all of it (rate limits and 5xx are retried)."""
        pending: Call | None = call
        for attempt in range(WRITE_ATTEMPTS):
            assert pending is not None
            response = await pending.send(self.http, self.url, self.headers)
            if response.status_code == 429 or response.status_code >= 500:
                self.counters["write_retries"] += 1
                await asyncio.sleep(min(5.0, 0.5 * (attempt + 1)))
                continue
            pending = self.retry_of(pending, response)
            self.counters["writes"] += 1
            if pending is None:
                return
            self.counters["write_retries"] += 1
            await asyncio.sleep(0.5)
        raise RuntimeError(f"{self.title} kept refusing a write after {WRITE_ATTEMPTS} attempts")

    async def seed(self, case: Case, ids: Identities) -> None:
        async with self._limit:
            for call in [*self.ensure_calls(case, ids), *self.seed_calls(case, ids)]:
                await self.write(call)

    def render(self, lines: Sequence[str]) -> str:
        body = [f"- {line}" for line in lines if line.strip()]
        if not body:
            return ""
        return "\n".join([self.header, *body]) if self.header else "\n".join(body)

    async def read(self, case: Case, ids: Identities, question: str) -> tuple[httpx.Response, float]:
        call = self.read_call(case, ids, question)
        watch = Stopwatch()
        response = await call.send(self.http, self.url, self.headers)
        elapsed = watch.ms
        response.raise_for_status()
        self.counters["reads"] += 1
        return response, elapsed

    async def retrieve(self, case: Case, ids: Identities, *, view: str | None = None) -> Retrieved:  # noqa: ARG002
        async with self._limit:
            try:
                response, elapsed = await self.read(case, ids, case.probe.question)
                lines = self.memories(response)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                return Retrieved(text="", elapsed_ms=0.0, error=type(exc).__name__)
        return Retrieved(text=self.render(lines), elapsed_ms=elapsed, meta={"results": len(lines)})

    # Metrics 1, 6, 7, 8 and 9

    def classify(self, response: httpx.Response) -> str:
        return OK if self.memories(response) else EMPTY

    def read_probe(self, pairs: Sequence[tuple[Case, Identities]]) -> latency.Probe:
        """Metric 1: the read of a turn, over the seeded customers, with each case's probe question."""
        calls = [self.read_call(case, ids, case.probe.question) for case, ids in pairs]
        base, headers = self.url, self.headers

        def call(client: httpx.AsyncClient, n: int) -> Awaitable[tuple[float, str]]:
            return timed(calls[n % len(calls)].send(client, base, headers), 200)

        return latency.Probe(self.system, HOST_PATH, call, self._factory())

    def _factory(self) -> Callable[[], httpx.AsyncBaseTransport] | None:
        transport = self.transport
        return (lambda: transport) if transport is not None else None

    async def history_operations(self, pairs: Sequence[tuple[Case, Identities]]) -> list[Operation]:
        """Metric 8: the read as `search`, and `open_call` on what a read returned as `open`."""
        reads = [self.read_call(case, ids, case.probe.question) for case, ids in pairs]
        base, headers = self.url, self.headers
        opens: list[Call] = []
        for (case, ids), read in zip(pairs, reads, strict=True):
            try:
                response = await read.send(self.http, base, headers)
                if response.status_code == 200 and (item := self.open_call(case, ids, response)):
                    opens.append(item)
            except (httpx.HTTPError, ValueError, KeyError):
                continue

        async def search(client: httpx.AsyncClient, n: int) -> tuple[float, str]:
            return await timed(reads[n % len(reads)].send(client, base, headers), 200, self.classify)

        ops = [
            Operation(
                latency.Probe(self.system, HOST_PATH, search, self._factory()), "search", _name(reads[0])
            )
        ]
        if self.has_open:

            async def open_one(client: httpx.AsyncClient, n: int) -> tuple[float, str]:
                return await timed(opens[n % len(opens)].send(client, base, headers), 200)

            skipped = None if opens else "no item returned by any read"
            name = _name(opens[0]) if opens else "open"
            probe = latency.Probe(self.system, HOST_PATH, open_one, self._factory())
            ops.append(Operation(probe, "open", name, skipped))
        return ops

    def ingest_operation(
        self,
        pairs: Sequence[tuple[Case, Identities]],
        tag: str,
        turns: int,
        exchange: Callable[[str, int], tuple[str, str]],
    ) -> Operation:
        """Metric 9: one exchange per call, `turns` per conversation, each with a new number."""
        sequence = itertools.count()
        base, headers = self.url, self.headers
        prefix = f"bench-{tag}-ing-{self.system}"

        async def call(client: httpx.AsyncClient, _n: int) -> tuple[float, str]:
            seq = next(sequence)
            case, ids = pairs[(seq // turns) % len(pairs)]
            customer, agent = exchange(case.language, seq)
            write = self.exchange_call(case, ids, f"{prefix}-conv-{seq // turns}", customer, agent)
            started = time.perf_counter()
            try:
                response = await write.send(client, base, headers)
            except httpx.HTTPError as exc:
                return (time.perf_counter() - started) * 1000, type(exc).__name__
            elapsed = (time.perf_counter() - started) * 1000
            if response.status_code // 100 != 2:
                return elapsed, f"http{response.status_code}"
            return elapsed, OK

        first = self.exchange_call(*pairs[0], f"{prefix}-conv-0", "", "")
        return Operation(latency.Probe(self.system, HOST_PATH, call, self._factory()), "write", _name(first))

    async def freshness_trial(
        self, case: Case, ids: Identities, interval_s: float, timeout_s: float
    ) -> float | None:
        """Metric 6: the customer's message is written, then read from another turn until it shows."""
        for call in self.ensure_calls(case, ids):
            await self.write(call)
        code = str(random.randint(100000, 999999))
        write = self.exchange_call(
            case,
            ids,
            f"bench-{ids.tag}-fresh-{case.id}",
            FRESH_MESSAGES[case.language].format(code=code),
            None,
        )
        started = time.perf_counter()
        await self.write(write)
        deadline = started + timeout_s
        while time.perf_counter() < deadline:
            try:
                response, _ = await self.read(case, ids, FRESH_QUERIES[case.language])
                if contains("\n".join(self.memories(response)), code):
                    return (time.perf_counter() - started) * 1000
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            await asyncio.sleep(interval_s)
        return None

    async def resilience_trials(
        self, proxy_url: str, pairs: Sequence[tuple[Case, Identities]]
    ) -> list[tuple[float, bool, bool]]:
        """Metric 7: the read through the fault proxy with an HTTP client at its defaults (5 s) and
        `raise_for_status()`: (milliseconds, raised, empty) per trial."""
        out: list[tuple[float, bool, bool]] = []
        async with httpx.AsyncClient() as client:
            for case, ids in pairs:
                call = self.read_call(case, ids, case.probe.question)
                started = time.perf_counter()
                raised = False
                empty = True
                try:
                    response = await call.send(client, proxy_url.rstrip("/"), self.headers)
                    response.raise_for_status()
                    empty = not self.memories(response)
                except Exception:
                    raised = True
                out.append(((time.perf_counter() - started) * 1000, raised, empty))
        return out


def _name(call: Call) -> str:
    return f"{call.method} {call.path}"
