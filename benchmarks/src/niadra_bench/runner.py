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
from niadra_bench.metrics import accuracy, cost, freshness, latency, resilience
from niadra_bench.targets.base import Target
from niadra_bench.targets.mem0 import Mem0LibTarget, Mem0PlatformTarget, Mem0RestTarget
from niadra_bench.targets.niadra import Keys, NiadraTarget
from niadra_bench.targets.reference import FullHistory, NoMemory

SCHEMA = "niadra-bench.results.v1"
METRICS = ("latency", "tokens", "cost", "accuracy", "privacy", "freshness", "resilience")
SYSTEMS = ("niadra", "mem0_oss", "mem0_oss_rerank", "mem0_platform")
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
    # Dry runs only: an in-process transport to niadra-mock instead of the network.
    niadra_transport: Callable[[], httpx.AsyncBaseTransport] | None = None
    niadra_base_url: str | None = None
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
        self.meter_url = os.environ.get("MEM0_METER_URL")

    # Targets

    def _niadra(self) -> NiadraTarget:
        keys = Keys.from_env()
        return NiadraTarget(
            keys,
            base_url=self.options.niadra_base_url,
            transport_factory=self.options.niadra_transport,
            settle_quiet_s=0 if self.options.dry_run else self.config.run.settle_quiet_s,
            settle_timeout_s=self.config.run.settle_timeout_s,
            concurrency=self.config.run.concurrency,
        )

    def build_targets(self) -> list[Target]:
        systems = self.options.systems
        targets: list[Target] = [NoMemory(), FullHistory()]
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
        limit = asyncio.Semaphore(self.config.run.concurrency)
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
        return {"seconds": round(time.monotonic() - started, 1), "failures": failures}

    async def repetition(self, n: int, targets: list[Target]) -> dict[str, Any]:
        tag = f"{self.run_id[-6:]}r{n}"
        pairs = [(case, Identities.for_case(case, tag)) for case in self.cases]
        metrics = self.options.metrics
        rep: dict[str, Any] = {"repetition": n, "tag": tag, "seed": {}, "settle": {}}
        log.info("repetition %s: seeding %s cases", n, len(pairs))

        mem0_usage: dict[str, dict[str, int]] = {}
        mem0_adds = 0
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

        rows: list[accuracy.CaseRow] = []
        rerank_usage: dict[str, dict[str, int]] = {}
        rerank_searches = 0
        if metrics & {"accuracy", "tokens", "privacy", "cost"}:
            agent = ContextOnlyAgent() if self.options.dry_run else Agent(self.chat, self.config.agent)
            judge = None if self.options.dry_run else Judge(self.chat, self.config.judge)
            for target in targets:
                before = await self._meter() if isinstance(target, Mem0LibTarget) else None
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
            valid, excluded = accuracy.valid_cases(rows, self.by_id)
            rep["validity"] = {"valid": len(valid), "excluded": excluded}
            rep["accuracy"] = accuracy.summarize(rows, valid)
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
            )

        niadra = next((t for t in targets if isinstance(t, NiadraTarget)), None)
        mem0 = next((t for t in targets if isinstance(t, Mem0RestTarget) and t.scenario == "known_id"), None)
        lat = self.config.latency
        quick = self.options.quick
        if "latency" in metrics:
            probes = self._latency_probes(niadra, mem0, pairs[: lat.conversations], tag)
            rep["latency"] = await latency.run(
                probes,
                [lat.rates[0]] if quick else lat.rates,
                3.0 if quick else lat.duration_s,
                min(lat.conversations, len(pairs)),
                lat.request_timeout_s,
            )
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
            )
        return rep

    def _latency_probes(
        self,
        niadra: NiadraTarget | None,
        mem0: Mem0RestTarget | None,
        pairs: list[tuple[Case, Identities]],
        tag: str,
    ) -> list[latency.Probe]:
        probes: list[latency.Probe] = []
        if niadra is not None:
            key = niadra.keys.for_channel("voice")[1]
            paths = {"edge": niadra.client("voice").base_url}
            if cluster := os.environ.get("NIADRA_CLUSTER_URL"):
                paths["cluster"] = cluster
            for path, base in paths.items():
                probe = latency.niadra_probe(path, base, key, pairs, tag)
                probe.transport = self.options.niadra_transport
                probes.append(probe)
        if mem0 is not None:
            probes.append(
                latency.mem0_probe(
                    mem0.url, mem0.api_key, pairs, self.config.mem0.top_k, self.config.mem0.threshold
                )
            )
        return probes

    def _write_rows(self, n: int, rows: Sequence[accuracy.CaseRow]) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / f"cases-rep{n}.jsonl").open("w") as handle:
            for row in rows:
                handle.write(json.dumps(row.dump(), ensure_ascii=False) + "\n")

    async def execute(self) -> Path:
        targets = self.build_targets()
        for target in targets:
            await target.start()
        try:
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
                "machine_class": identity.get("instance_type")
                or (env.machine_class if kind == "region" else None),
                "database_class": env.database_class if kind == "region" else None,
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
                "hash": bench_config.config_hash(),
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
            },
            "dataset": {
                "hash": generate.dataset_hash(bench_config.DATASET_DIR),
                "generator_version": generate.GENERATOR_VERSION,
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
    return metrics


def load_cases() -> list[Case]:
    return generate.load(bench_config.DATASET_DIR)
