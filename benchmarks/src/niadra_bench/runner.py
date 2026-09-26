"""One benchmark run: every repetition seeds fresh customers into every system, waits for them to be
readable, and measures. Results go to `results/<date>-<run id>/`:

- `summary.json`: what the site reads, with the run's metadata and every metric across repetitions;
- `rep-<n>.json`: each repetition's raw metric output;
- `cases-rep<n>.jsonl`: one line per system, case and view, with the memory block's size, the agent's
  answer and both grades, so every accuracy number can be audited.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx

from niadra_bench import __version__, stats
from niadra_bench import config as bench_config
from niadra_bench.agent import Agent, ChatClient, ContextOnlyAgent, Judge
from niadra_bench.config import BenchConfig
from niadra_bench.dataset import generate
from niadra_bench.dataset.model import Case
from niadra_bench.identity import SCENARIOS, Identities
from niadra_bench.metrics import accuracy, cost, freshness, history, ingest, latency, operations, resilience
from niadra_bench.net import niadra_routes
from niadra_bench.sources import ControlPlane, dataset_operations
from niadra_bench.systems import REGISTRY, HttpSystem
from niadra_bench.targets.base import Target
from niadra_bench.targets.mem0 import Mem0LibTarget, Mem0PlatformTarget, Mem0RestTarget, exchanges
from niadra_bench.targets.niadra import Keys, NiadraTarget
from niadra_bench.targets.reference import FullHistory, NoMemory

SCHEMA = "niadra-bench.results.v1"
METRICS = ("latency", "tokens", "cost", "accuracy", "privacy", "freshness", "resilience", "history", "ingest")
BUILT_IN = ("niadra", "mem0_oss", "mem0_oss_rerank", "mem0_platform")
#: Every system `--systems` accepts: the built-in targets and one per adapter in `niadra_bench.systems`.
SYSTEMS = (*BUILT_IN, *REGISTRY)
log = logging.getLogger("niadra_bench")


@dataclass
class Options:
    systems: set[str]
    metrics: set[str]
    repetitions: int
    dry_run: bool = False
    limit: int | None = None
    quick: bool = False
    output: Path | None = None
    # The dataset version the cases come from (config.DATASET_VERSIONS).
    dataset: str = bench_config.DEFAULT_DATASET
    # Niadra's `memory_v2` space flag for the run: on, off, or None to leave the space as it is.
    memory_v2: bool | None = None
    # Dry runs only: an in-process transport to niadra-mock instead of the network.
    niadra_transport: Callable[[], httpx.AsyncBaseTransport] | None = None
    niadra_base_url: str | None = None
    # A bootstrap document of Niadra keys instead of NIADRA_BOOTSTRAP (`bench ab` against a local cell).
    niadra_bootstrap: Path | None = None
    # The instant Niadra's history is seeded from (default: the clock at each seeding).
    niadra_now: datetime | None = None
    # Seconds of no change the settle step waits for (default: `run.settle_quiet_s`, 0 in a dry run).
    settle_quiet_s: float | None = None
    # The two references of the validity rule (no memory, full history). `bench ab` asks them once per
    # repetition, on its baseline side, and grades both sides on the same valid cases.
    references: bool = True
    # What every repetition's tag starts with (default: the end of the run id). The tag makes the
    # customers' handles, so two runs with the same tag seed the same people.
    tag: str | None = None
    # Tests only: an in-process transport per system added through `niadra_bench.systems`.
    system_transports: dict[str, httpx.AsyncBaseTransport] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


def _git_commit() -> str:
    if value := os.environ.get("BENCH_HARNESS_COMMIT"):
        return value
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            cwd=bench_config.ROOT,
            timeout=5,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _package(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


async def _instance_identity() -> dict[str, str]:
    """Region and instance type from the instance metadata service, when a pod can reach it."""
    base = "http://169.254.169.254/latest"
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            token = (
                await client.put(f"{base}/api/token", headers={"x-aws-ec2-metadata-token-ttl-seconds": "60"})
            ).text
            headers = {"x-aws-ec2-metadata-token": token}
            document = (
                await client.get(f"{base}/dynamic/instance-identity/document", headers=headers)
            ).json()
        return {"region": document["region"], "instance_type": document["instanceType"]}
    except (httpx.HTTPError, ValueError, KeyError):
        return {}


def _tokenizer(encoding: str) -> Callable[[str], int]:
    import tiktoken

    enc = tiktoken.get_encoding(encoding)
    return lambda text: len(enc.encode(text, disallowed_special=()))


class Run:
    def __init__(self, config: BenchConfig, cases: list[Case], options: Options) -> None:
        self.config = config
        # A smoke run's --limit takes cases spread over the dataset, so every category and language shows up.
        self.cases = (
            cases[:: max(1, len(cases) // options.limit)][: options.limit] if options.limit else cases
        )
        self.by_id = {c.id: c for c in self.cases}
        self.options = options
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.started = datetime.now(UTC)
        self.chat = ChatClient()
        self.tokenizer: Callable[[str], int] = options.extra.get("tokenizer") or _tokenizer(
            config.tokens.encoding
        )
        root = options.output or bench_config.RESULTS_DIR
        self.out = root / f"{self.started:%Y-%m-%d}-{self.run_id[-6:]}"
        self.reps: list[dict[str, Any]] = []
        # Each repetition's per-case rows, for `bench ab`.
        self.rows: dict[int, list[accuracy.CaseRow]] = {}
        self.meter_url = os.environ.get("MEM0_METER_URL")

    # Targets

    @property
    def against_production(self) -> bool:
        """Whether Niadra here is the production cell, so the caps of config [production] apply: a run in
        the region (`BENCH_ENVIRONMENT=region`) that reaches Niadra over the network, whatever its agent
        (a `bench ab` with the context agent is a dry run of the agent, not of Niadra). niadra-mock in the
        process, a local run and `bench ab --local-cell` (its own bootstrap) keep the run's own settings."""
        options = self.options
        return (
            os.environ.get("BENCH_ENVIRONMENT") == "region"
            and options.niadra_transport is None
            and options.niadra_bootstrap is None
        )

    def _niadra(self) -> NiadraTarget:
        options = self.options
        keys = Keys.from_env(
            {"NIADRA_BOOTSTRAP": str(options.niadra_bootstrap)} if options.niadra_bootstrap else None
        )
        quiet = options.settle_quiet_s
        if quiet is None:
            quiet = 0 if options.dry_run else self.config.run.settle_quiet_s
        now = options.niadra_now
        caps = self.config.production if self.against_production else None
        return NiadraTarget(
            keys,
            base_url=options.niadra_base_url,
            transport_factory=options.niadra_transport,
            now=(lambda: now) if now is not None else None,
            settle_quiet_s=quiet,
            settle_timeout_s=self.config.run.settle_timeout_s,
            concurrency=caps.read_concurrency if caps else self.config.run.concurrency,
            control_url=ControlPlane.available(keys.document),
            operations=dataset_operations(self.cases),
            memory_v2=self.options.memory_v2,
            seed_rate=caps.seed_batches_per_s if caps else None,
            seed_concurrency=caps.seed_concurrency if caps else None,
            settle_interval_s=caps.settle_interval_s if caps else 0.0,
            pause_on_overload_s=caps.pause_on_overload_s if caps else 0.0,
        )

    def _niadra_rates(self, rates: list[int], capped: list[int]) -> list[int]:
        """Niadra's rates for a timed loop: the production cap, or the first rate of a quick run."""
        chosen = capped if self.against_production else rates
        return chosen[:1] if self.options.quick else chosen

    def build_targets(self) -> list[Target]:
        systems = self.options.systems
        targets: list[Target] = [NoMemory(), FullHistory()] if self.options.references else []
        if "niadra" in systems:
            targets.append(self._niadra())
        for scenario in SCENARIOS:
            if "mem0_oss" in systems:
                targets.append(
                    Mem0RestTarget(scenario, self.config.mem0, concurrency=self.config.run.concurrency)
                )
            if "mem0_oss_rerank" in systems:
                targets.append(Mem0LibTarget(scenario, self.config.mem0))
            if "mem0_platform" in systems:
                targets.append(Mem0PlatformTarget(scenario, self.config.mem0))
        for key, adapter in REGISTRY.items():
            if key in systems:
                targets.append(
                    adapter(
                        transport=self.options.system_transports.get(key),
                        concurrency=self.config.run.concurrency,
                        settle_timeout_s=self.config.run.settle_timeout_s,
                    )
                )
        return targets

    async def _meter(self) -> dict[str, Any] | None:
        if not self.meter_url:
            return None
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.meter_url.rstrip('/')}/_meter")
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data

    # Phases

    async def _seed(self, target: Target, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:
        limit = asyncio.Semaphore(target.seed_concurrency or self.config.run.concurrency)
        started = time.monotonic()
        failures: list[str] = []

        async def one(case: Case, ids: Identities) -> None:
            async with limit:
                try:
                    await target.seed(case, ids)
                except Exception as exc:
                    failures.append(f"{case.id}: {type(exc).__name__}: {str(exc)[:200]}")

        await asyncio.gather(*(one(c, i) for c, i in pairs))
        if len(failures) > max(2, len(pairs) // 50):
            raise RuntimeError(f"seeding {target.label} failed for {len(failures)} cases: {failures[:3]}")
        return {"seconds": round(time.monotonic() - started, 1), "failures": failures, **target.seed_report()}

    async def repetition(self, n: int, targets: list[Target]) -> dict[str, Any]:
        tag = f"{self.options.tag or self.run_id[-6:]}r{n}"
        pairs = [(case, Identities.for_case(case, tag)) for case in self.cases]
        metrics = self.options.metrics
        rep: dict[str, Any] = {"repetition": n, "tag": tag, "seed": {}, "settle": {}}
        log.info("repetition %s: seeding %s cases", n, len(pairs))

        mem0_usage: dict[str, dict[str, int]] = {}
        mem0_adds = 0
        others = [t for t in targets if isinstance(t, HttpSystem)]
        # Each added system's model gateway, before its seeding and after its memory settled.
        meters_before = {o.system: await o.meter_snapshot() for o in others}
        for target in targets:
            if not target.seeds or isinstance(target, NoMemory | FullHistory):
                continue
            before = await self._meter() if isinstance(target, Mem0RestTarget) else None
            rep["seed"][target.label] = await self._seed(target, pairs)
            if isinstance(target, Mem0RestTarget):
                delta = cost.meter_delta(before, await self._meter())
                rep["seed"][target.label]["model_usage"] = delta
                if target.scenario == "known_id":
                    mem0_usage = delta
                    mem0_adds = target.counters["add_infer"]
                    target.counters["add_infer"] = 0
            log.info("seeded %s in %ss", target.label, rep["seed"][target.label]["seconds"])
        for target in targets:
            if target.seeds:
                rep["settle"][target.label] = await target.settle(pairs)
        spend: dict[str, dict[str, dict[str, int]]] = {}
        for other in others:
            if meters_before[other.system] is not None:
                spend[other.system] = cost.meter_delta(
                    meters_before[other.system], await other.meter_snapshot()
                )
                rep["seed"][other.label]["model_usage"] = spend[other.system]

        rows: list[accuracy.CaseRow] = []
        rerank_usage: dict[str, dict[str, int]] = {}
        rerank_searches = 0
        # What an added system's reads cost in models (a system that reasons on every read), per read.
        read_spend: dict[str, tuple[dict[str, dict[str, int]], int]] = {}
        if metrics & {"accuracy", "tokens", "privacy", "cost"}:
            agent = ContextOnlyAgent() if self.options.dry_run else Agent(self.chat, self.config.agent)
            judge = None if self.options.dry_run else Judge(self.chat, self.config.judge)
            for target in targets:
                before = await self._meter() if isinstance(target, Mem0LibTarget) else None
                gateway = await target.meter_snapshot() if isinstance(target, HttpSystem) else None
                log.info("accuracy pass: %s", target.label)
                rows += await accuracy.run(
                    [target],
                    pairs,
                    repetition=n,
                    agent=agent,
                    judge=judge,
                    tokenizer=self.tokenizer,
                    niadra_views=self.config.niadra.views,
                    concurrency=self.config.run.concurrency,
                )
                if isinstance(target, Mem0LibTarget) and target.scenario == "known_id":
                    rerank_usage = cost.meter_delta(before, await self._meter())
                    rerank_searches = len(pairs)
                if isinstance(target, HttpSystem) and gateway is not None:
                    read_spend[target.system] = (
                        cost.meter_delta(gateway, await target.meter_snapshot()),
                        len(pairs),
                    )
            valid, excluded = accuracy.valid_cases(rows, self.by_id)
            rep["validity"] = {"valid": len(valid), "excluded": excluded}
            rep["accuracy"] = accuracy.summarize(rows, valid)
            self.rows[n] = rows
            self._write_rows(n, rows)

        if "cost" in metrics and rep.get("accuracy"):
            tokens = {}
            for line in rep["accuracy"]:
                if line["scenario"] in (None, "known_id"):
                    view = "chat" if "chat" in line["tokens"] else "default"
                    tokens[line["system"]] = line["tokens"].get(view, {}).get("median")
            rep["cost"] = cost.compute(
                self.config,
                tokens_per_turn=tokens,
                mem0_seed_usage=mem0_usage,
                mem0_infer_adds=mem0_adds,
                rerank_usage=rerank_usage,
                rerank_searches=rerank_searches,
                platform_measured="mem0_platform" in self.options.systems,
                others=[
                    (o.system, row) for o in others if (row := self._cost_row(o, spend, read_spend, pairs))
                ],
            )

        niadra = next((t for t in targets if isinstance(t, NiadraTarget)), None)
        mem0 = next((t for t in targets if isinstance(t, Mem0RestTarget) and t.scenario == "known_id"), None)
        lat = self.config.latency
        caps = self.config.production
        quick = self.options.quick
        if "latency" in metrics:
            rep["latency"] = []
            duration = 3.0 if quick else lat.duration_s
            count = min(lat.conversations, len(pairs))
            remote = self._niadra_probes(niadra, pairs[: lat.conversations], tag)
            local = self._local_probes(mem0, others, pairs[: lat.conversations])
            niadra_rates = self._niadra_rates(lat.rates, caps.latency_rates)
            rates = [lat.rates[0]] if quick else lat.rates
            rep["latency"] += await latency.run(remote, niadra_rates, duration, count, lat.request_timeout_s)
            rep["latency"] += await latency.run(local, rates, duration, count, lat.request_timeout_s)
        if "freshness" in metrics:
            fresh = self.config.freshness
            rep["freshness"] = await freshness.run(
                niadra,
                mem0,
                self.cases,
                tag,
                3 if quick else fresh.trials,
                fresh.poll_interval_ms,
                2.0 if quick else fresh.timeout_s,
                others=others,
            )
        if "resilience" in metrics:
            res = self.config.resilience
            key = niadra.keys.for_channel("voice")[1] if niadra else None
            upstream = (self.options.niadra_base_url or niadra.client("voice").base_url) if niadra else None
            transport = self.options.niadra_transport() if self.options.niadra_transport else None
            rep["resilience"] = await resilience.run(
                niadra_upstream=upstream,
                niadra_key=key,
                mem0_upstream=mem0.url if mem0 else None,
                mem0_key=mem0.api_key if mem0 else "",
                pairs=pairs[: 3 if quick else res.trials],
                tag=tag,
                delay_ms=res.delay_ms,
                status=res.status,
                budget_ms=res.turn_budget_ms,
                top_k=self.config.mem0.top_k,
                threshold=self.config.mem0.threshold,
                transport=transport,
                others=others,
            )
        # Metrics 8 and 9 run last, so the earlier metrics see the same conditions as in earlier runs.
        if "history" in metrics:
            his = self.config.history
            chosen = pairs[: his.conversations]
            rep["history"] = await history.run(
                niadra,
                mem0,
                chosen,
                tag,
                his,
                self.config.mem0,
                rates=[his.rates[0]] if quick else his.rates,
                niadra_rates=self._niadra_rates(his.rates, caps.history_rates),
                duration_s=3.0 if quick else his.duration_s,
                transport=self.options.niadra_transport,
                others=[op for other in others for op in await other.history_operations(chosen)],
            )
        if "ingest" in metrics:
            ing = self.config.ingest
            chosen = pairs[: ing.conversations]
            turns = ing.turns_per_conversation
            rep["ingest"] = await ingest.run(
                niadra,
                mem0,
                chosen,
                tag,
                ing,
                rates=[ing.rates[0]] if quick else ing.rates,
                niadra_rates=self._niadra_rates(ing.rates, caps.ingest_rates),
                duration_s=3.0 if quick else ing.duration_s,
                cooldown_s=0.0 if quick else ing.cooldown_s,
                transport=self.options.niadra_transport,
                others=[o.ingest_operation(chosen, tag, turns, ingest.exchange) for o in others],
            )
        return rep

    def _cost_row(
        self,
        system: HttpSystem,
        spend: dict[str, dict[str, dict[str, int]]],
        read_spend: dict[str, tuple[dict[str, dict[str, int]], int]],
        pairs: list[tuple[Case, Identities]],
    ) -> dict[str, Any] | None:
        """Metric 3 for an added system: its adapter's line, or the model spend its gateway counted:
        from seeding until the memory settled, per exchange written, plus the accuracy pass's reads, per
        read (zero unless the system calls a model to read); one write and one read per exchange, for a
        thousand conversations."""
        if (row := system.cost_row()) is not None:
            return row
        usage = spend.get(system.system)
        written = sum(len(exchanges(s)) for case, _ in pairs for s in case.sessions if s.turns)
        if usage is None or not written:
            return None
        per_exchange = cost.model_usd(self.config, usage) / written
        read_usage, reads = read_spend.get(system.system, ({}, 0))
        per_read = cost.model_usd(self.config, read_usage) / reads if reads else 0.0
        turns = self.config.cost.turns_per_conversation
        return {
            "variant": "models_only",
            "memory_usd_per_1000": round((per_exchange + per_read) * turns * 1000, 4),
            "basis": "measured model spend (writes until the memory settled, and reads), servers not priced",
            "usd_per_exchange": round(per_exchange, 8),
            "usd_per_read": round(per_read, 8),
        }

    def _niadra_probes(
        self, niadra: NiadraTarget | None, pairs: list[tuple[Case, Identities]], tag: str
    ) -> list[latency.Probe]:
        """Niadra's context over each path (net.niadra_routes), at the production caps."""
        if niadra is None:
            return []
        key = niadra.keys.for_channel("voice")[1]
        edge = niadra.client("voice").base_url
        probes = []
        for route in niadra_routes(edge, "NIADRA_CLUSTER_URL", edge_transport=self.options.niadra_transport):
            probe = latency.niadra_probe(route.path, route.base, key, pairs, tag)
            probe.transport = route.transport
            probes.append(probe)
        return probes

    def _local_probes(
        self, mem0: Mem0RestTarget | None, others: list[HttpSystem], pairs: list[tuple[Case, Identities]]
    ) -> list[latency.Probe]:
        """Every system on the harness's host: Mem0's search and each added system's read."""
        probes: list[latency.Probe] = []
        if mem0 is not None:
            probes.append(
                latency.mem0_probe(
                    mem0.url, mem0.api_key, pairs, self.config.mem0.top_k, self.config.mem0.threshold
                )
            )
        return probes + [other.read_probe(pairs) for other in others]

    def _write_rows(self, n: int, rows: Sequence[accuracy.CaseRow]) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / f"cases-rep{n}.jsonl").open("w") as handle:
            for row in rows:
                handle.write(json.dumps(row.dump(), ensure_ascii=False) + "\n")

    async def execute(self) -> Path:
        targets = self.build_targets()
        try:
            # Inside the try: a target that started (a billing key issued, a flag set) is closed even
            # when a later one fails to start, or the run is stopped (bench run turns SIGTERM into a
            # cancellation, so this block still runs).
            for target in targets:
                await target.start()
            for n in range(1, self.options.repetitions + 1):
                rep = await self.repetition(n, targets)
                self.reps.append(rep)
                self.out.mkdir(parents=True, exist_ok=True)
                (self.out / f"rep-{n}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
            versions: dict[str, str] = {}
            for target in targets:
                versions.update(target.versions())
        finally:
            for target in targets:
                await target.close()
            await self.chat.close()
        summary = await self.summary(versions)
        (self.out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        return self.out

    async def summary(self, versions: dict[str, str]) -> dict[str, Any]:
        kind = "dry-run" if self.options.dry_run else os.environ.get("BENCH_ENVIRONMENT", "local")
        dataset_dir = bench_config.dataset_dir(self.options.dataset)
        memory_v2 = self.options.memory_v2
        # Where the harness ran, from the instance metadata; Niadra's machine and database are the
        # cell's (config [environment]): the harness runs on a host of its own since 26/09/2026.
        identity = await _instance_identity() if kind == "region" else {}
        env = self.config.environment
        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "date": f"{self.started:%Y-%m-%d}",
            "started_at": self.started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "environment": {
                "kind": kind,
                "region": identity.get("region") or (env.region if kind == "region" else None),
                "machine_class": env.machine_class if kind == "region" else None,
                "database_class": env.database_class if kind == "region" else None,
                "harness_host": env.harness_host if kind == "region" else None,
                "harness_machine_class": identity.get("instance_type"),
                "verified_by_instance_metadata": bool(identity),
                "host": socket.gethostname() if kind != "region" else None,
                "python": platform.python_version(),
            },
            "versions": {
                "harness": __version__,
                "harness_commit": _git_commit(),
                "niadra_sdk": _package("niadra"),
                "niadra_server": os.environ.get("BENCH_NIADRA_SERVER_VERSION"),
                "mem0ai": _package("mem0ai"),
                **{k: v for k, v in versions.items() if k not in ("niadra_sdk", "mem0ai")},
            },
            "config": {
                "hash": bench_config.config_hash(dataset=self.options.dataset),
                "repetitions": self.options.repetitions,
                "agent_model": self.config.agent.model,
                "judge_model": None if self.options.dry_run else self.config.judge.model,
                "agent": "context_only" if self.options.dry_run else "llm",
                "extraction_model": self.config.models.extraction,
                "embedder": self.config.models.embedder,
                "latency_rates": self.config.latency.rates,
                "latency_duration_s": self.config.latency.duration_s,
                "latency_conversations": self.config.latency.conversations,
                "mem0_top_k": self.config.mem0.top_k,
                "mem0_threshold": self.config.mem0.threshold,
                "token_encoding": self.config.tokens.encoding,
                "turns_per_conversation": self.config.cost.turns_per_conversation,
                "prices_checked_on": self.config.prices.checked_on,
                "history_rates": self.config.history.rates,
                "history_duration_s": self.config.history.duration_s,
                "history_conversations": self.config.history.conversations,
                "history_max_tokens": self.config.history.max_tokens,
                "ingest_rates": self.config.ingest.rates,
                "ingest_duration_s": self.config.ingest.duration_s,
                "ingest_conversations": self.config.ingest.conversations,
                "ingest_mem0_modes": self.config.ingest.mem0_modes,
                # Whether the run set Niadra's `memory_v2` space flag, and to what ("unchanged": the
                # space kept its own setting, as in every run before the flag existed).
                "niadra_memory_v2": "unchanged" if memory_v2 is None else ("on" if memory_v2 else "off"),
                # `context_has_answer` by whole token for values of three or more digits and by the
                # category's pattern otherwise; the first run's rule is `context_has_answer_loose`.
                "context_has_answer_rule": "specific-v2",
            },
            "dataset": {
                "version": self.options.dataset,
                "hash": generate.dataset_hash(dataset_dir),
                "generator_version": generate.GENERATOR_VERSIONS[self.options.dataset],
                "cases": len(self.cases),
                "valid": stats.across([r.get("validity", {}).get("valid") for r in self.reps], 0),
                "excluded": [r.get("validity", {}).get("excluded", []) for r in self.reps],
            },
            "metrics": aggregate(self.reps),
        }


def _group(
    reps: list[dict[str, Any]], metric: str, key: Callable[[dict[str, Any]], tuple[Any, ...]]
) -> dict[tuple[Any, ...], list[dict[str, Any] | None]]:
    """Lines of one metric by key, one slot per repetition (None when a repetition lacks it)."""
    keys: dict[tuple[Any, ...], list[dict[str, Any] | None]] = {}
    for index, rep in enumerate(reps):
        for line in rep.get(metric) or []:
            keys.setdefault(key(line), [None] * len(reps))[index] = line
    return keys


def _pick(lines: list[dict[str, Any] | None], *path: str) -> list[float | None]:
    out: list[float | None] = []
    for line in lines:
        value: Any = line
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        out.append(value if isinstance(value, int | float) else None)
    return out


def aggregate(reps: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    if any("latency" in r for r in reps):
        rows = []
        for (system, path, rate), lines in _group(
            reps, "latency", lambda x: (x["system"], x["path"], x["rate"])
        ).items():
            rows.append(
                {
                    "system": system,
                    "path": path,
                    "rate": rate,
                    **{p: stats.across(_pick(lines, p), 1) for p in ("p50", "p95", "p99", "max")},
                    "errors": sum(sum((line or {}).get("errors", {}).values()) for line in lines),
                    "sent": sum((line or {}).get("sent", 0) for line in lines),
                }
            )
        metrics["latency"] = {"unit": "ms", "results": rows}
    if any("accuracy" in r for r in reps):
        acc_rows, tok_rows, priv_rows = [], [], []
        for (system, scenario), lines in _group(
            reps, "accuracy", lambda x: (x["system"], x["scenario"])
        ).items():
            acc_rows.append(
                {
                    "system": system,
                    "scenario": scenario,
                    "cases": stats.across(_pick(lines, "cases"), 0),
                    "judge": stats.across(_pick(lines, "judge"), 4),
                    "deterministic": stats.across(_pick(lines, "deterministic"), 4),
                    "context_has_answer": stats.across(_pick(lines, "context_has_answer"), 4),
                    "by_category": {
                        category: {
                            "judge": stats.across(_pick(lines, "by_category", category, "judge"), 4),
                            "deterministic": stats.across(
                                _pick(lines, "by_category", category, "deterministic"), 4
                            ),
                        }
                        for category in next((x for x in lines if x), {}).get("by_category", {})
                    },
                    "retrieve_errors": sum((line or {}).get("retrieve_errors", 0) for line in lines),
                    # Runs before the rule changed have no loose figure; their summary stays as published.
                    **(
                        {
                            "context_has_answer_loose": stats.across(
                                _pick(lines, "context_has_answer_loose"), 4
                            )
                        }
                        if any("context_has_answer_loose" in (line or {}) for line in lines)
                        else {}
                    ),
                }
            )
            views = next((x for x in lines if x), {}).get("tokens", {})
            for view in views:
                tok_rows.append(
                    {
                        "system": system,
                        "scenario": scenario,
                        "view": view,
                        "median": stats.across(_pick(lines, "tokens", view, "median"), 1),
                        "p95": stats.across(_pick(lines, "tokens", view, "p95"), 1),
                    }
                )
            priv_rows.append(
                {
                    "system": system,
                    "scenario": scenario,
                    **(
                        {"verification": first_privacy["verification"]}
                        if "verification"
                        in (first_privacy := next((x for x in lines if x), {}).get("privacy", {}))
                        else {}
                    ),
                    "cases": stats.across(_pick(lines, "privacy", "cases"), 0),
                    "leaks": stats.across(_pick(lines, "privacy", "leaks"), 0),
                    "leak_rate": stats.across(_pick(lines, "privacy", "leak_rate"), 4),
                }
            )
        metrics["accuracy"] = {"unit": "share of valid cases", "results": acc_rows}
        metrics["tokens"] = {"unit": "tokens per turn", "results": tok_rows}
        metrics["privacy"] = {"unit": "memory blocks with the sensitive value", "results": priv_rows}
    if any("cost" in r for r in reps):
        rows = []
        for (system, variant), lines in _group(reps, "cost", lambda x: (x["system"], x["variant"])).items():
            first = next((x for x in lines if x), {})
            rows.append(
                {
                    "system": system,
                    "variant": variant,
                    "basis": first.get("basis"),
                    "memory_usd_per_1000": stats.across(_pick(lines, "memory_usd_per_1000"), 4),
                    "agent_prompt_usd_per_1000": stats.across(_pick(lines, "agent_prompt_usd_per_1000"), 4),
                    **(
                        {"conversations_per_month": first["conversations_per_month"]}
                        if "conversations_per_month" in first
                        else {}
                    ),
                }
            )
        metrics["cost"] = {"unit": "USD per 1000 conversations", "results": rows}
    if any("freshness" in r for r in reps):
        rows = []
        for (system,), lines in _group(reps, "freshness", lambda x: (x["system"],)).items():
            rows.append(
                {
                    "system": system,
                    **{p: stats.across(_pick(lines, p), 1) for p in ("p50", "p95", "max")},
                    "trials": sum((line or {}).get("trials", 0) for line in lines),
                    "timeouts": sum((line or {}).get("timeouts", 0) for line in lines),
                }
            )
        metrics["freshness"] = {"unit": "ms", "results": rows}
    if any("resilience" in r for r in reps):
        rows = []
        for (system, fault), lines in _group(reps, "resilience", lambda x: (x["system"], x["fault"])).items():
            rows.append(
                {
                    "system": system,
                    "fault": fault,
                    "memory_stage_p50_ms": stats.across(_pick(lines, "memory_stage_ms", "p50"), 1),
                    "memory_stage_max_ms": stats.across(_pick(lines, "memory_stage_ms", "max"), 1),
                    "within_budget_rate": stats.across(_pick(lines, "within_budget_rate"), 4),
                    "raised_rate": stats.across(_pick(lines, "raised_rate"), 4),
                    "empty_rate": stats.across(_pick(lines, "empty_rate"), 4),
                    "turn_budget_ms": next((x for x in lines if x), {}).get("turn_budget_ms"),
                }
            )
        metrics["resilience"] = {"results": rows}
    for name in ("history", "ingest"):
        if (summary := operations.aggregate(reps, name)) is not None:
            metrics[name] = summary
    return metrics


def load_cases(version: str = bench_config.DEFAULT_DATASET) -> list[Case]:
    return generate.load(bench_config.dataset_dir(version))
