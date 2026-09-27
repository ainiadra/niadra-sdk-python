"""A local cell for `bench ab --local-cell`: niadra-back's in-memory flow harness behind its own HTTP routes.

Not part of the harness package: it runs inside a niadra-back checkout, with that checkout's environment
(`uv run --project <checkout>`) and the checkout as the working directory, so it serves the backend code
of that checkout, as the Q1 and WP2 rounds replayed the benchmark. Every service and task handler is the
real one (`tests/unit/flow/harness.py`): the batch route ingests, then runs the pipeline's tasks until
none is left (session close, the extraction gate, extraction, memory, compilation), so the answer comes
after the memory is built and a read right after it sees everything. Each benchmark customer gets a cell
of its own (`Shards`), so the in-memory store stays small.

What stands in for the cloud, the same on both sides of an A/B:
- the extraction model: a rule extractor (`RuleExtractor`) that summarizes a session with the
  customer's ask and each line that settles a new number, and picks the category and intent by
  keywords; it never extracts facts, so what a pack holds comes from episodes, the deterministic value
  net, system records and actions;
- the decisions port (Jev, estudo 14): niadra-back's own `DeterministicDecider`, the fake its unit tests
  wire behind `DecideService` (`niadra.testing.cell.Cell`) for the extraction gate, injection, sensitive
  category, question type and contradiction tasks, by keyword, deterministically, no network;
- the models server: niadra-back's `DeterministicModels` (hash embeddings of 64 dimensions, regex PII);
- the clock: fixed at `--now`, the instant the harness also seeds from.

The process environment picks the variant, as the read deployment's would:
- `NIADRA_SEMANTIC_CHANNEL` off/models: `models` gives the read path the query encoder, here the hash
  encoder above (`inprocess` needs the model files and is refused);
- `NIADRA_SEMANTIC_DEADLINE_MS`: the semantic channel's deadline;
- `NIADRA_BENCH_GUARD_TYPES` (comma list of value types, e.g. `amount,date,protocol`; empty by default):
  every customer's cell starts with one contradiction of each type measured today in
  `context_use_guard_daily` (the guards arm), which is what makes a space write guard lines for
  those types; without it a space writes none until its measurement has counted agents contradicting
  them (in the region, `bench run --niadra-guards measure` records the answers that measurement reads).
Any other `NIADRA_*` variable stays in the environment for backend code that reads it directly.

With `--real-models` the extraction model and the decisions port are production's instead: niadra-back's
own `OpenRouterLlm` (GPT-6 Luna, at the effort `app/extract.py` asks for, `low`) and `JevDecider`
(`typesafe/jev-1.13`, the questions and thresholds of `domain/decide`), built with their default settings,
which are the ones the region's deployment runs (no provider order, no base URL override), and the same
extraction settings. They call OpenRouter with the key in `OPENROUTER_API_KEY` (`bench ab --real-models`
reads the benchmark's key from Secrets Manager into it; it is never logged or written). What stays local:
the models server (hash embeddings, regex PII), the clock and the in-memory store. Two things differ from
the region on purpose: the space's daily AI ceiling is not enforced (the benchmark space's is raised past
what a run spends), and an extraction the model failed is retried at once instead of after its backoff,
since the clock does not move. Every model call goes through `model_cache.CachingTransport`: with
`--model-cache <dir>` an answer already on disk for the same model, prompt version and request body is
read from there, and every new answer is written there; `GET /_bench/models` returns the calls, the
answers read from disk, and the spend OpenRouter reported.

Numbers from this cell are never published: one process, and no real models unless `--real-models`.

    uv run --project ../niadra-back python deploy/local/cell_server.py --port 18900 \
        --now 2026-09-25T12:00:00+00:00 --keys-out /tmp/cell-keys.json --operations credit,refund
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import os
import pickle
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

sys.path[:0] = [os.getcwd()]

import uvicorn  # noqa: E402
from model_cache import CachingTransport, Ledger  # noqa: E402  (beside this file)
from niadra.adapters.inbound.http import ingest as ingest_routes  # noqa: E402
from niadra.adapters.inbound.http import read as read_routes  # noqa: E402
from niadra.adapters.inbound.http.app import create_app  # noqa: E402
from niadra.adapters.memory import uow as memory_uow  # noqa: E402
from niadra.adapters.memory.fakes import (  # noqa: E402
    DeterministicDecider,
    DeterministicModels,
    FixedClock,
    LlmResult,
    LlmUsage,
    StaticConfig,
)
from niadra.app.retrieval import RetrievalCache, RetrievalService  # noqa: E402
from niadra.config.models import SourceConfig, SpaceConfig, TenantSettings  # noqa: E402
from niadra.domain.common.errors import Unauthenticated  # noqa: E402
from niadra.domain.common.space import SourcePrincipal  # noqa: E402
from niadra.domain.extract.budget import SpendHold  # noqa: E402
from niadra.domain.pipeline.delay import DelayReason  # noqa: E402
from niadra.domain.vocabulary import Audience, Scope, Verification  # noqa: E402
from niadra.testing.cell import SPACE_ID, TENANT_ID, Cell, principal  # noqa: E402
from tests.unit.audit.fakes import RecordingAdminAudit  # noqa: E402
from tests.unit.flow.harness import Flow  # noqa: E402

SOURCES = {
    "whatsapp": (UUID("0192f5a0-0000-7000-8000-00000000c001"), "whatsapp"),
    "voice": (UUID("0192f5a0-0000-7000-8000-00000000c002"), "voice"),
    "billing": (UUID("0192f5a0-0000-7000-8000-00000000c003"), "erp"),
}
RETRIEVAL_CACHE_BYTES = 128 << 20


class _PickleCopy:
    """The in-memory unit of work snapshots its store with `copy.deepcopy` to open every transaction;
    a pickle round trip makes the same deep copy about three times faster on a customer's store. Whatever
    does not pickle is copied as before."""

    @staticmethod
    def deepcopy(value: Any) -> Any:
        try:
            return pickle.loads(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))  # noqa: S301
        except (pickle.PicklingError, TypeError, AttributeError):
            return copy.deepcopy(value)


memory_uow.copy = _PickleCopy  # type: ignore[assignment]


def _flag(name: str, default: str) -> str:
    return os.environ.get(name, default).strip().lower()


GUARD_TYPES = tuple(t.strip() for t in os.environ.get("NIADRA_BENCH_GUARD_TYPES", "").split(",") if t.strip())


async def seed_guards(flow: Flow) -> None:
    """One contradiction of each of `GUARD_TYPES` measured today on the voice source, in the guards arm:
    the row `context_use_guard_daily` holds when agents contradicted a value of that type."""
    from niadra.domain.measure.slots import GuardCounters, GuardDailyKey

    day = flow.cell.clock.now().date()
    async with flow.cell.uow(SPACE_ID) as uow:
        for value_type in GUARD_TYPES:
            key = GuardDailyKey(day, SOURCES["voice"][0], value_type, "guards")
            await uow.measure.add_guard_daily(key, GuardCounters(slots=1, contradicted=1))


_TURN = re.compile(r"^\[e\d+[^\]]*\] \d\d:\d\d (\w+): (.*)$")
_NUMBER = re.compile(r"\d+")
_VALUE = re.compile(r"\b[\w$.,/-]*\d{3,}[\w.,/-]*")
_TECHNICAL = re.compile(r"internet|conex|sinal|fibra|wi-?fi|rede|connection|signal|outage|modem|router", re.I)
_ORDERS = re.compile(r"pedido|entrega|order|deliver|package|pacote|encomenda|rastreio|tracking", re.I)
_COMPLAINT = re.compile(
    r"de novo|mais uma vez|again|voltou a|caiu|caindo|sem internet|without internet|problema|problem|"
    r"reclam|complain|não funciona|not working|atras|late|quebr|broke|went down|dropp|outra vez|"
    r"falt|missing|falhou|failed|could not|demor|too long|recusad|declined|did not|não chegou|lost",
    re.I,
)


class RuleExtractor:
    """Stands in for the extraction model, deterministically: the episode summary is the customer's ask
    and each later line that says a number not said before, as written (the prompt asks the model to
    keep identifiers and values exactly as written; when a prompt does not, the values are dropped).
    Category and intent come from keywords. No facts, open items or profile lines."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, **kw: Any) -> LlmResult:
        self.calls += 1
        turns: list[tuple[str, str]] = []
        for line in kw["user"].splitlines():
            if match := _TURN.match(line):
                body = match.group(2)
                if body.startswith('"'):
                    with contextlib.suppress(ValueError):
                        body = json.loads(body.split(" [o")[0])
                turns.append((match.group(1), body))
        bodies = [body for _, body in turns]
        customer = " ".join(body for who, body in turns if who == "customer")
        ask = bodies[0][:70] if bodies else ""
        seen = set(_NUMBER.findall(ask))
        said: list[str] = []
        for body in bodies[1:]:
            numbers = set(_NUMBER.findall(body))
            if numbers - seen:
                said.append(body[:70])
                seen |= numbers
        if not said and len(bodies) > 1:
            said = [bodies[1][:70]]
        summary = " / ".join([ask, *said])[:220]
        if "exactly as written" not in kw["system"]:
            summary = _VALUE.sub("", summary)
        episode = kw["json_schema"]["properties"]["episode"]["properties"]
        categories = episode["category"].get("enum") or ["other"]
        category = (
            "technical" if _TECHNICAL.search(customer) else "orders" if _ORDERS.search(customer) else "other"
        )
        if category not in categories:
            category = "other" if "other" in categories else categories[0]
        intents = episode["intent"].get("enum") or ["other"]
        intent = "complaint" if _COMPLAINT.search(customer) and "complaint" in intents else "request"
        content = json.dumps(
            {
                "episode": {
                    "summary": summary[:400],
                    "intent": intent,
                    "category": category,
                    "outcome": "resolved",
                    "resolution": None,
                    "sentiment": 0.0,
                },
                "facts": [],
                "open_items": [],
                "resolve": [],
                "profile": [],
                "agent_questions": [],
                "agent_claims": [],
            }
        )
        usage = LlmUsage(input_tokens=len(kw["user"]) // 4, output_tokens=len(content) // 4)
        return LlmResult(content=content, model="rule-extractor", provider="local", usage=usage)


class LocalSpend:
    """The daily AI budget's port without a ceiling: reserves nothing, refuses nothing. The spend is counted
    from OpenRouter's usage numbers by the transport's `Ledger` instead."""

    def __init__(self, clock: FixedClock) -> None:
        self._clock = clock

    async def reserve(self, space_id: UUID, purpose: str, estimate_micros: int | None = None) -> SpendHold:  # noqa: ARG002
        return SpendHold(space_id, purpose, self._clock.now().date(), 0)

    async def settle(self, hold: SpendHold, cost_micros: int) -> None:  # noqa: ARG002
        return None

    async def release(self, hold: SpendHold) -> None:  # noqa: ARG002
        return None


class RealModels:
    """Production's Luna and Jev adapters over one caching transport, shared by every customer's cell as
    one worker process shares them (Jev's concurrency of 8 included)."""

    def __init__(self, clock: FixedClock, cache: Path | None) -> None:
        import httpx
        from niadra.adapters.outbound.llm import jev, openrouter
        from niadra.app import extract
        from niadra.domain.extract.rules import EXTRACTOR_VERSION, PROMPT_VERSION

        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise SystemExit("--real-models needs OPENROUTER_API_KEY (bench ab --real-models sets it)")
        self.ledger = Ledger()
        self.cache = cache
        self.prompt_version = f"{EXTRACTOR_VERSION}.{PROMPT_VERSION}"
        transport = CachingTransport(
            httpx.AsyncHTTPTransport(), cache, self.ledger, default_version=self.prompt_version
        )
        budget = LocalSpend(clock)
        luna_settings = openrouter.OpenRouterSettings()
        jev_settings = jev.JevSettings()
        self.llm = openrouter.OpenRouterLlm(
            api_key,
            budget,
            luna_settings,
            http=httpx.AsyncClient(transport=transport, timeout=luna_settings.timeout_s),
        )
        self.decider = jev.JevDecider(
            api_key,
            budget,
            jev_settings,
            http=httpx.AsyncClient(transport=transport, timeout=jev_settings.timeout_s),
        )
        del api_key
        self.applied = {
            "extractor": f"{openrouter.MODEL} (reasoning {extract.REASONING})",
            "decisions": jev.MODEL,
            "prompt_version": self.prompt_version,
            "model_cache": str(cache) if cache else "off",
        }

    def report(self) -> dict[str, Any]:
        return {"ledger": self.ledger.snapshot(), "model_cache": str(self.cache) if self.cache else None}


async def retry_failed_extractions(flow: Flow) -> None:
    """The region retries an extraction whose model call failed after a backoff (`LLM_RETRY`); the fixed
    clock never reaches it, so the cell moves those retries to the outbox at once and drains again, until
    none is left (each one counts its attempts, so this ends at the task's last attempt)."""
    rows = flow.cell.uow.store.module("pipeline").setdefault("scheduled", [])
    while retries := [row for row in rows if row[2] == DelayReason.LLM_RETRY]:
        rows[:] = [row for row in rows if row[2] != DelayReason.LLM_RETRY]
        flow.cell.uow.store.module("pipeline").setdefault("outbox", []).extend(task for task, _, _ in retries)
        await flow.drain()


def space_config(operations: frozenset[str]) -> SpaceConfig:
    """The benchmark's sandbox space as the harness sets it up: the starter policy and extraction schema,
    a WhatsApp and a voice source, and the billing agent's own source declaring the dataset's operations
    (`src/niadra_bench/sources.py`)."""
    ids = {name: source_id for name, (source_id, _) in SOURCES.items()}
    return SpaceConfig(
        space_id=SPACE_ID,
        tenant_id=TENANT_ID,
        region="us-east-2",
        environment="sandbox",
        settings=TenantSettings(),
        sources=[
            SourceConfig(
                source_id=ids["whatsapp"],
                name="whatsapp",
                channel="whatsapp",
                purposes=frozenset({"support"}),
                audience=Audience.CUSTOMER_AGENT,
            ),
            SourceConfig(
                source_id=ids["voice"],
                name="voice",
                channel="voice",
                purposes=frozenset({"support"}),
                audience=Audience.CUSTOMER_AGENT,
                verification_ceiling=Verification.V3,
            ),
            SourceConfig(
                source_id=ids["billing"],
                name="billing",
                channel="erp",
                purposes=frozenset({"billing"}),
                audience=Audience.INTERNAL_AGENT,
                trusted_action_ops=operations,
            ),
        ],
    )


class LocalEdge:
    """Key check and rate limits of the edge: one key per source, no limit."""

    def __init__(self, keys: dict[str, SourcePrincipal]) -> None:
        self.keys = keys

    async def authenticate_key(self, token: str) -> SourcePrincipal:
        if token not in self.keys:
            raise Unauthenticated("unknown key")
        return self.keys[token]

    async def authenticate_person(self, token: str) -> SourcePrincipal:  # noqa: ARG002
        raise Unauthenticated("the local cell takes source keys only")

    async def check_rate(self, caller: SourcePrincipal, path: str) -> None:  # noqa: ARG002
        return None

    async def check_webhook_rate(self, source_id: UUID, path: str) -> None:  # noqa: ARG002
        return None


_CASE = re.compile(r"(?:^|-)((?:pt|en)-\d{3})(?=-|$)")
_TAG = re.compile(r"^bench-([a-z0-9]+)-")
SHARED = "_shared"


def _handle_key(handle: Any) -> tuple[str, str] | None:
    if handle is None:
        return None
    kind = getattr(handle, "type", None)
    value = getattr(handle, "value", None)
    return (str(getattr(kind, "value", kind)), str(value)) if value is not None else None


class Shards:
    """One in-memory cell per benchmark customer, all with the same space, settings and clock.

    The in-memory unit of work copies its whole store to open each transaction (its rollback), so one
    store for every customer of a run makes seeding quadratic: a dataset v2 customer of 60 sessions takes
    minutes after a few dozen others. Each customer's memory depends only on that customer's events, so a
    cell per customer serves the same packs, as the WP2 replay did with a fresh cell per case. What a
    space learns across customers (the nightly retrieval weights) does not run here either way.

    A request goes to its customer's cell by the case id the harness puts in every idempotency key and
    conversation id (`bench-<tag>-<case>-...`; the tag is the repetition's, so each repetition's
    customer of a case has a cell of its own), or else by a handle an earlier batch of that customer
    carried; anything else goes to a shared cell."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self.flows: dict[str, Flow] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.handles: dict[tuple[str, str], str] = {}
        self.conversations: dict[str, str] = {}

    def flow(self, key: str) -> Flow:
        if key not in self.flows:
            self.flows[key] = self._factory()
            self.locks[key] = asyncio.Lock()
        return self.flows[key]

    def key(self, *, texts: list[str | None], handles: list[Any]) -> str:
        for text in texts:
            if text and (match := _CASE.search(text)):
                tag = _TAG.match(text)
                return f"{tag.group(1) if tag else ''}:{match.group(1)}"
            if text and text in self.conversations:
                return self.conversations[text]
        for handle in handles:
            if (known := _handle_key(handle)) and known in self.handles:
                return self.handles[known]
        return SHARED

    def learn(self, key: str, *, conversations: list[str | None], handles: list[Any]) -> None:
        if key == SHARED:
            return
        for conversation in conversations:
            if conversation:
                self.conversations[conversation] = key
        for handle in handles:
            if known := _handle_key(handle):
                self.handles[known] = key


def _request_parts(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[list[str | None], list[Any]]:
    texts: list[str | None] = [kwargs.get("conversation_id")]
    handles: list[Any] = [kwargs.get("subject")]
    for arg in args:
        texts.append(getattr(arg, "conversation_id", None))
        handles += [getattr(arg, "subject", None), getattr(arg, "about", None)]
    return texts, handles


class ServeRouter:
    """The read service of the customer's cell."""

    def __init__(self, shards: Shards) -> None:
        self._shards = shards

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            texts, handles = _request_parts(args[1:], kwargs)
            flow = self._shards.flow(self._shards.key(texts=texts, handles=handles))
            return getattr(flow.serve, name)(*args, **kwargs)

        return call


class IngestRouter:
    """The ingest service of the customer's cell, but a batch answers after the pipeline ran every task
    it started (session close, the gate, extraction, memory, compilation)."""

    def __init__(self, shards: Shards, retry_extractions: bool = False) -> None:
        self._shards = shards
        self._guards_seeded: set[str] = set()
        self._retry = retry_extractions

    async def ingest_items(self, caller: SourcePrincipal, items: Any, rejected: Any = (), **kw: Any) -> Any:
        parsed = [item for _, item in items]
        texts: list[str | None] = []
        conversations: list[str | None] = []
        handles: list[Any] = []
        for item in parsed:
            texts += [getattr(item, "idempotency_key", None), getattr(item, "conversation_id", None)]
            conversations.append(getattr(item, "conversation_id", None))
            handles += [*(getattr(item, "handles", None) or []), getattr(item, "handle", None)]
        key = self._shards.key(texts=texts, handles=handles)
        self._shards.learn(key, conversations=conversations, handles=handles)
        flow = self._shards.flow(key)
        async with self._shards.locks[key]:
            if GUARD_TYPES and key not in self._guards_seeded:
                await seed_guards(flow)
                self._guards_seeded.add(key)
            result = await flow.ingest.ingest_items(caller, items, rejected, **kw)
            await flow.drain()
            if self._retry:
                await retry_failed_extractions(flow)
            return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._shards.flow(SHARED).ingest, name)


class ExtractRouter:
    def __init__(self, shards: Shards) -> None:
        self._shards = shards

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            texts, handles = _request_parts(args[1:], kwargs)
            flow = self._shards.flow(self._shards.key(texts=texts, handles=handles))
            return getattr(flow.extract, name)(*args, **kwargs)

        return call


def build(
    now: datetime, operations: frozenset[str], models: RealModels | None = None
) -> tuple[Any, dict[str, Any]]:
    semantic = _flag("NIADRA_SEMANTIC_CHANNEL", "off")
    if semantic not in ("off", "models"):
        raise SystemExit(
            f"NIADRA_SEMANTIC_CHANNEL={semantic}: the local cell runs `off` or `models` (no model files here)"
        )
    deadline_ms = float(os.environ.get("NIADRA_SEMANTIC_DEADLINE_MS", "30"))
    config = StaticConfig(space_config(operations))
    clock = FixedClock(now)

    def new_flow() -> Flow:
        decider = models.decider if models else DeterministicDecider()
        cell = Cell(clock=clock, config=config, models=DeterministicModels(), decider=decider)  # type: ignore[arg-type]
        flow = Flow.build(cell, models.llm if models else RuleExtractor())  # type: ignore[arg-type]
        if semantic == "models":
            flow.serve.retrieval = RetrievalService(
                cell.uow,
                cell.config,
                cell.clock,
                RetrievalCache(RETRIEVAL_CACHE_BYTES),
                cell.models,
                deadline_s=deadline_ms / 1000,
            )
        return flow

    shards = Shards(new_flow)
    space = config.space(SPACE_ID)
    keys: dict[str, str] = {}
    principals: dict[str, SourcePrincipal] = {}
    for name, (source_id, _) in SOURCES.items():
        key = f"nia_sk_test_local_cell_{name}1_localcellsecret"
        keys[name] = key
        principals[key] = principal(source_id, space, frozenset(Scope) - {Scope.ADMIN, Scope.ANALYTICS})
    container = SimpleNamespace(
        edge=LocalEdge(principals),
        serve=ServeRouter(shards),
        ingest=IngestRouter(shards, retry_extractions=models is not None),
        extract=ExtractRouter(shards),
        clock=clock,
        admin_audit=RecordingAdminAudit(),
        config=config,
    )
    app = create_app("local-cell", container, [read_routes.router, ingest_routes.router])
    if models is not None:
        app.add_api_route("/_bench/models", models.report, methods=["GET"], include_in_schema=False)
    applied = {
        "semantic_channel": semantic,
        "semantic_encoder": "hash-64" if semantic == "models" else None,
        "semantic_deadline_ms": deadline_ms,
        "guard_types_seeded": list(GUARD_TYPES),
        "models": "real" if models else "local",
        "extractor": "rule-extractor",
        "decisions": "deterministic-decider",
        **(models.applied if models else {}),
        "cells": "one per customer",
        "now": now.isoformat(),
        "operations": sorted(operations),
        "env": {k: v for k, v in sorted(os.environ.items()) if k.startswith("NIADRA_") and not _secret(k)},
    }
    return app, {"keys": keys, "cell": applied}


def _secret(name: str) -> bool:
    return any(word in name for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--now", required=True, help="the cell's fixed clock, ISO 8601 with offset")
    parser.add_argument("--keys-out", required=True, help="where to write the bootstrap document")
    parser.add_argument("--operations", default="", help="the billing source's operations, comma list")
    parser.add_argument(
        "--real-models", action="store_true", help="production's Luna and Jev adapters over OpenRouter"
    )
    parser.add_argument(
        "--model-cache", default=None, help="with --real-models: the directory of cached answers (none: off)"
    )
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now)
    operations = frozenset(o for o in args.operations.split(",") if o)
    models = None
    if args.real_models:
        models = RealModels(FixedClock(now), Path(args.model_cache) if args.model_cache else None)
    app, document = build(now, operations, models)
    out = Path(args.keys_out)
    out.write_text(json.dumps(document, indent=2) + "\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
