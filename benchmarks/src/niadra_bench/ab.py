"""`bench ab`: a baseline and a candidate over the same prepared dataset, and the delta between them.

Both sides answer the same cases, repetition by repetition (baseline 1, candidate 1, baseline 2, ...),
with everything else equal: the same frozen configuration unless `--candidate-config` overrides sections
of it, the same agent and judge, the same seeding order. The candidate differs by `--candidate-env
KEY=VALUE` (repeatable), a setting of the Niadra server under test, such as `NIADRA_MEMORY_V2=on` or
`NIADRA_SEMANTIC_CHANNEL=models`. A key the candidate sets and the baseline does not is set on the
baseline too, to its default (`DEFAULTS`), so both sides say what they ran with.

Where the two sides run:

- `--local-cell <niadra-back checkout>`: each side is a local cell of its own
  (`deploy/local/cell_server.py`: niadra-back's in-memory flow harness behind its HTTP routes, a rule
  extractor in place of the model), started with the side's settings in its process environment. Both
  cells get the same clock and the same customers, so any difference comes from the setting. No network,
  no key; the numbers are for the team and never published.
- `--mock`: both sides are niadra-mock in-process. It has no server settings, so only `--same` runs;
  it is the check the CI runs.
- neither: the Niadra of NIADRA_BOOTSTRAP (the region Job). There the harness can change only what the
  bootstrap's admin account changes through the control API, the space's settings (`SPACE_SETTINGS`):
  `NIADRA_MEMORY_V2`. A setting of the read deployment's process is refused, because the region's read
  deployment also serves production. The two sides seed different customers (the same cases) into the
  same space, one after the other.

`--same` is the determinism check: the candidate is the baseline again. Every accuracy figure and every
token count must come out identical, case by case and in the summary; latency may differ. The run fails
(exit code 1) and lists what differed otherwise.

Results go to `results/ab/<date>-<id>/` (a local cell or the emulator: `results/local/ab/`, never
committed): `ab.json` (`niadra-bench.ab.v1`: both sides' settings and metrics, the delta, the case flips
and the determinism verdict), `ab.md` (the tables), and each side's `rep-<n>.json` and `cases-rep<n>.jsonl`
under `baseline/` and `candidate/`. No `summary.json`: the site's importer never takes an A/B.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import socket
import subprocess
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from niadra_bench import config as bench_config
from niadra_bench import report_ab
from niadra_bench.config import BenchConfig
from niadra_bench.dataset import generate
from niadra_bench.dataset.model import Case
from niadra_bench.metrics import accuracy
from niadra_bench.runner import Options, Run, aggregate
from niadra_bench.sources import dataset_operations
from niadra_bench.targets.base import Target

SCHEMA = "niadra-bench.ab.v1"
AB_CONFIG = bench_config.CONFIG_DIR / "ab.toml"
CELL_SERVER = bench_config.ROOT / "deploy" / "local" / "cell_server.py"
LOCAL_RESULTS = bench_config.RESULTS_DIR / "local" / "ab"
#: The value a side runs with when it does not set a key the other side sets: the server's default.
DEFAULTS = {
    "NIADRA_MEMORY_V2": "off",
    "NIADRA_SEMANTIC_CHANNEL": "off",
    # A local cell's seeded guard types (deploy/local/cell_server.py): none by default.
    "NIADRA_BENCH_GUARD_TYPES": "",
}
#: Keys that are a space setting, which the harness sets through the control API in the region.
SPACE_SETTINGS = {"NIADRA_MEMORY_V2": "memory_v2"}
#: What `bench ab` measures unless `--metrics` says otherwise: accuracy, context_has_answer, tokens,
#: privacy and cost from one pass, and the latency of metrics 1, 8 and 9.
DEFAULT_METRICS = ("accuracy", "tokens", "privacy", "cost", "latency", "history", "ingest")
_TRUE = ("on", "true", "1", "yes")
_FALSE = ("off", "false", "0", "no")

log = logging.getLogger("niadra_bench")


class AbError(ValueError):
    """An A/B that cannot run as asked."""


@dataclass(frozen=True)
class AbSettings:
    """`config/ab.toml`: kept apart from benchmark.toml so the published runs' configuration hash stays."""

    max_minutes: float
    local_settle_quiet_s: float

    @classmethod
    def load(cls, path: Path = AB_CONFIG) -> AbSettings:
        with path.open("rb") as handle:
            ab = tomllib.load(handle)["ab"]
        return cls(float(ab["max_minutes"]), float(ab["local"]["settle_quiet_s"]))


@dataclass
class Side:
    name: str
    env: dict[str, str]
    config: BenchConfig
    config_hash: str
    config_file: str | None = None
    run: Run | None = None
    reps: list[dict[str, Any]] = field(default_factory=list)
    rows: dict[int, list[accuracy.CaseRow]] = field(default_factory=dict)


def parse_env(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum() or key.upper() != key:
            raise AbError(f"{pair!r} is not KEY=VALUE")
        out[key] = value.strip()
    return out


def sides_env(
    baseline: dict[str, str], candidate: dict[str, str], *, same: bool, config: bool = False
) -> tuple[dict[str, str], dict[str, str]]:
    """Both sides' settings: a key only the candidate sets gets its default on the baseline."""
    if same:
        if candidate or config:
            raise AbError("--same compares the baseline with itself: no candidate env, config or cell")
        return dict(baseline), dict(baseline)
    if not candidate and not config:
        raise AbError("an A/B needs --candidate-env, --candidate-config or --candidate-cell (or --same)")
    base = dict(baseline)
    for key in candidate:
        if key not in base:
            if key not in DEFAULTS:
                raise AbError(f"{key} has no known default: give the baseline's value with --baseline-env")
            base[key] = DEFAULTS[key]
    return base, dict(candidate) | {k: v for k, v in base.items() if k not in candidate}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        nested = isinstance(value, dict) and isinstance(out.get(key), dict)
        out[key] = _merge(out[key], value) if nested else value
    return out


def candidate_config(path: Path, config_dir: Path = bench_config.CONFIG_DIR) -> tuple[BenchConfig, str]:
    """benchmark.toml with the sections of `path` laid over it, and a hash of both files."""
    with (config_dir / "benchmark.toml").open("rb") as handle:
        base = tomllib.load(handle)
    with path.open("rb") as handle:
        override = tomllib.load(handle)
    config = BenchConfig.model_validate(_merge(base, override))
    digest = bench_config.config_hash(config_dir)
    return config, digest + "+" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _flag(value: str) -> bool:
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise AbError(f"{value!r} is not on or off")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class LocalCell:
    """One side's local cell: `deploy/local/cell_server.py` in a niadra-back checkout, with the side's
    settings in its process environment and nothing else of the harness's NIADRA_* environment."""

    def __init__(
        self, checkout: Path, env: dict[str, str], now: datetime, operations: list[str], workdir: Path
    ) -> None:
        self.checkout = checkout
        self.env = env
        self.now = now
        self.operations = operations
        self.workdir = workdir
        self.port = 0
        self.process: asyncio.subprocess.Process | None = None
        self.document: dict[str, Any] = {}
        # The checkout's commit when the cell started: the code it imported, even if the checkout moves.
        self.commit: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def bootstrap(self) -> Path:
        return self.workdir / "cell.json"

    def command(self) -> list[str]:
        return [
            "uv", "run", "--frozen", "--quiet", "--project", str(self.checkout), "--with", "numpy>=2.1",
            "python", str(CELL_SERVER), "--port", str(self.port), "--now", self.now.isoformat(),
            "--keys-out", str(self.bootstrap), "--operations", ",".join(self.operations),
        ]  # fmt: skip

    def process_env(self) -> dict[str, str]:
        inherited = {k: v for k, v in os.environ.items() if not k.startswith("NIADRA_")}
        inherited.pop("VIRTUAL_ENV", None)
        return inherited | self.env

    async def start(self, timeout_s: float = 180.0) -> None:
        if not (self.checkout / "tests" / "unit" / "flow" / "harness.py").exists():
            raise AbError(f"{self.checkout} is not a niadra-back checkout (no tests/unit/flow/harness.py)")
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.commit = _commit(self.checkout)
        self.port = _free_port()
        log_file = (self.workdir / "cell.log").open("w")
        self.process = await asyncio.create_subprocess_exec(
            *self.command(),
            cwd=self.checkout,
            env=self.process_env(),
            stdout=log_file,
            stderr=log_file,
        )
        deadline = time.monotonic() + timeout_s
        async with httpx.AsyncClient(timeout=1.0) as client:
            while time.monotonic() < deadline:
                if self.process.returncode is not None:
                    raise AbError(f"the local cell exited; see {self.workdir / 'cell.log'}")
                with contextlib.suppress(httpx.HTTPError):
                    if (await client.get(f"{self.base_url}/healthz")).status_code == 200:
                        self.document = json.loads(self.bootstrap.read_text())
                        return
                await asyncio.sleep(0.5)
        await self.stop()
        raise AbError(f"the local cell did not answer in {timeout_s:.0f}s; see {self.workdir / 'cell.log'}")

    async def stop(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), 15)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()


@dataclass
class AbOptions:
    baseline_env: dict[str, str]
    candidate_env: dict[str, str]
    same: bool = False
    candidate_config: Path | None = None
    dataset: str = bench_config.DEFAULT_DATASET
    metrics: set[str] = field(default_factory=lambda: set(DEFAULT_METRICS))
    repetitions: int | None = None
    limit: int | None = None
    quick: bool = False
    agent: str | None = None  # "context" or "llm"; default: context on a local cell or the mock
    local_cell: Path | None = None
    # The candidate's own niadra-back checkout (a branch against the baseline's), on a local cell.
    candidate_cell: Path | None = None
    mock: bool = False
    output: Path | None = None
    now: datetime | None = None
    max_minutes: float | None = None
    label: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def environment_kind(options: AbOptions) -> str:
    if options.mock:
        return "dry-run"
    if options.local_cell:
        return "local-cell"
    return os.environ.get("BENCH_ENVIRONMENT", "local")


class Ab:
    def __init__(self, cases: list[Case], options: AbOptions, settings: AbSettings | None = None) -> None:
        self.options = options
        self.settings = settings or AbSettings.load()
        self.cases = cases
        self.config = bench_config.load()
        base_env, cand_env = sides_env(
            options.baseline_env,
            options.candidate_env,
            same=options.same,
            config=options.candidate_config is not None or options.candidate_cell is not None,
        )
        self._check_where(base_env | cand_env)
        base_hash = bench_config.config_hash(dataset=options.dataset)
        cand_config, cand_hash, cand_file = self.config, base_hash, None
        if options.candidate_config:
            cand_config, cand_hash = candidate_config(options.candidate_config)
            cand_file = str(options.candidate_config)
        self.baseline = Side("baseline", base_env, self.config, base_hash)
        self.candidate = Side("candidate", cand_env, cand_config, cand_hash, cand_file)
        self.started = datetime.now(UTC)
        self.ab_id = self.started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.kind = environment_kind(options)
        default_root = LOCAL_RESULTS if self.kind != "region" else bench_config.RESULTS_DIR / "ab"
        self.out = (options.output or default_root) / f"{self.started:%Y-%m-%d}-{self.ab_id[-6:]}"
        # The instant both sides seed from (and a local cell's clock stands at): the start of the hour.
        self.now = options.now or self.started.replace(minute=0, second=0, microsecond=0)
        self.cells: dict[str, LocalCell] = {}
        self.valid: dict[int, set[str]] = {}
        self.incomplete: str | None = None

    def _check_where(self, env: dict[str, str]) -> None:
        if self.options.candidate_cell is not None and self.options.local_cell is None:
            raise AbError("--candidate-cell is the candidate's checkout beside --local-cell's")
        if self.options.mock and env:
            raise AbError("niadra-mock has no server settings: with --mock, only --same runs")
        if self.options.local_cell or self.options.mock:
            return
        process = sorted(k for k in env if k not in SPACE_SETTINGS)
        if process:
            raise AbError(
                f"{', '.join(process)}: a setting of the read deployment's process, which in the region "
                "also serves production; run this A/B with --local-cell, or set it on a deployment "
                "of its own"
            )

    def _agent_is_llm(self) -> bool:
        if self.options.agent:
            return self.options.agent == "llm"
        return not (self.options.local_cell or self.options.mock)

    def _run_for(self, side: Side) -> Run:
        opts = self.options
        memory_v2: bool | None = None
        if not opts.local_cell and not opts.mock and "NIADRA_MEMORY_V2" in side.env:
            memory_v2 = _flag(side.env["NIADRA_MEMORY_V2"])
        # A local cell per side holds only that side's customers, so both seed the same people; in the
        # region both sides share the space and seed different people.
        same_people = bool(opts.local_cell or opts.mock)
        tag = self.ab_id[-6:] if same_people else f"{self.ab_id[-5:]}{side.name[0]}"
        options = Options(
            systems={"niadra"},
            metrics=set(opts.metrics),
            repetitions=1,
            dry_run=not self._agent_is_llm(),
            limit=opts.limit,
            quick=opts.quick,
            dataset=opts.dataset,
            memory_v2=memory_v2,
            references=side.name == "baseline",
            tag=tag,
            # Both sides seed from the same instant, so every event carries the same time on both: a
            # time in the pack never differs between them (the exact check's loose match reads the
            # minutes of a time as a day).
            niadra_now=self.now,
            settle_quiet_s=self.settings.local_settle_quiet_s if opts.local_cell else None,
            extra=dict(opts.extra),
        )
        if opts.mock:
            from niadra_mock import MOCK_KEY, MockApp

            mock = MockApp()
            options.niadra_transport = lambda: httpx.ASGITransport(app=mock.asgi)
            options.niadra_base_url = "http://niadra-mock"
            bootstrap = self.out / side.name / "mock.json"
            bootstrap.parent.mkdir(parents=True, exist_ok=True)
            bootstrap.write_text(json.dumps({"keys": {"whatsapp": MOCK_KEY}}) + "\n")
            options.niadra_bootstrap = bootstrap
        run = Run(side.config, self.cases, options)
        run.out = self.out / side.name
        return run

    async def _start_cell(self, side: Side) -> None:
        assert self.options.local_cell is not None
        operations = dataset_operations(self.cases)
        checkout = self.options.local_cell
        if side is self.candidate and self.options.candidate_cell is not None:
            checkout = self.options.candidate_cell
        cell = LocalCell(checkout, side.env, self.now, operations, self.out / side.name)
        await cell.start()
        self.cells[side.name] = cell
        assert side.run is not None
        side.run.options.niadra_base_url = cell.base_url
        side.run.options.niadra_bootstrap = cell.bootstrap

    async def _repetition(self, side: Side, n: int) -> None:
        run = side.run
        assert run is not None
        targets: list[Target] = run.build_targets()
        for target in targets:
            await target.start()
        try:
            rep = await run.repetition(n, targets)
        finally:
            for target in targets:
                await target.close()
        rep["repetition"] = n
        side.reps.append(rep)
        side.rows[n] = run.rows.get(n, [])

    def _regrade(self, n: int) -> None:
        """Both sides graded on the cases the baseline's references make valid in this repetition."""
        base_rows = self.baseline.rows.get(n, [])
        by_id = {c.id: c for c in self.baseline.run.cases} if self.baseline.run else {}
        valid, excluded = accuracy.valid_cases(base_rows, by_id)
        self.valid[n] = valid
        references = [r for r in base_rows if r.system in ("no_memory", "full_history")]
        for side in (self.baseline, self.candidate):
            rows = side.rows.get(n, [])
            rep = side.reps[-1]
            if "accuracy" not in rep:
                continue
            graded = rows if side is self.baseline else references + rows
            rep["validity"] = {"valid": len(valid), "excluded": excluded}
            rep["accuracy"] = accuracy.summarize(graded, valid)
            rep["context_by_category"] = report_ab.context_by_category(rows, valid)

    def _write(self, side: Side) -> None:
        directory = self.out / side.name
        directory.mkdir(parents=True, exist_ok=True)
        for rep in side.reps:
            path = directory / f"rep-{rep['repetition']}.json"
            path.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")

    async def execute(self) -> Path:
        opts = self.options
        repetitions = opts.repetitions or self.config.run.repetitions
        ceiling = (opts.max_minutes or self.settings.max_minutes) * 60
        started = time.monotonic()
        self.out.mkdir(parents=True, exist_ok=True)
        for side in (self.baseline, self.candidate):
            side.run = self._run_for(side)
        try:
            if opts.local_cell:
                for side in (self.baseline, self.candidate):
                    await self._start_cell(side)
            for n in range(1, repetitions + 1):
                elapsed = time.monotonic() - started
                if n > 1 and elapsed / (n - 1) * n > ceiling:
                    self.incomplete = (
                        f"stopped after {n - 1} of {repetitions} repetitions: the next would pass the "
                        f"ceiling of {ceiling / 60:.0f} minutes ([ab] max_minutes)"
                    )
                    log.warning(self.incomplete)
                    break
                for side in (self.baseline, self.candidate):
                    log.info("A/B repetition %s: %s %s", n, side.name, side.env or "(as deployed)")
                    await self._repetition(side, n)
                self._regrade(n)
                for side in (self.baseline, self.candidate):
                    self._write(side)
        finally:
            for cell in self.cells.values():
                await cell.stop()
            for side in (self.baseline, self.candidate):
                if side.run is not None:
                    await side.run.chat.close()
        document = self.document()
        (self.out / "ab.json").write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        (self.out / "ab.md").write_text(report_ab.markdown(document))
        return self.out

    def document(self) -> dict[str, Any]:
        base, cand = aggregate(self.baseline.reps), aggregate(self.candidate.reps)
        for metrics, side in ((base, self.baseline), (cand, self.candidate)):
            by_category = report_ab.aggregate_context(side.reps)
            if by_category:
                metrics["context_by_category"] = by_category
        cells = report_ab.cells(base, cand)
        flips = report_ab.flips(self.baseline.rows, self.candidate.rows, self.valid)
        determinism = None
        if self.options.same:
            determinism = report_ab.determinism(cells, self.baseline.rows, self.candidate.rows)
        dataset_dir = bench_config.dataset_dir(self.options.dataset)
        run = self.baseline.run
        return {
            "schema": SCHEMA,
            "ab_id": self.ab_id,
            "label": self.options.label,
            "date": f"{self.started:%Y-%m-%d}",
            "started_at": self.started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "mode": "same" if self.options.same else "ab",
            "environment": {
                "kind": self.kind,
                "publishable": False,
                "note": report_ab.NOTES.get(self.kind, report_ab.NOTES["region"]),
                "local_cell": self._cell_info(),
            },
            "agent": "llm" if self._agent_is_llm() else "context_only",
            "judge_model": self.config.judge.model if self._agent_is_llm() else None,
            "dataset": {
                "version": self.options.dataset,
                "hash": generate.dataset_hash(dataset_dir),
                "cases": len(run.cases) if run else 0,
                "valid": [r.get("validity", {}).get("valid") for r in self.baseline.reps],
            },
            "repetitions": len(self.baseline.reps),
            "incomplete": self.incomplete,
            "metrics_run": sorted(self.options.metrics),
            "baseline": self._side(self.baseline, base),
            "candidate": self._side(self.candidate, cand),
            "delta": cells,
            "flips": flips,
            "determinism": determinism,
        }

    def _cell_info(self) -> dict[str, Any] | None:
        if not self.cells:
            return None
        cell = next(iter(self.cells.values()))
        return {"checkout": str(cell.checkout), "now": self.now.isoformat(), "commit": cell.commit}

    def _side(self, side: Side, metrics: dict[str, Any]) -> dict[str, Any]:
        cell = self.cells.get(side.name)
        return {
            "env": side.env,
            "config_hash": side.config_hash,
            "config_file": side.config_file,
            "cell": cell.document.get("cell") if cell else None,
            "checkout": str(cell.checkout) if cell else None,
            "commit": cell.commit if cell else None,
            "seed": [rep.get("seed", {}).get("niadra") for rep in side.reps],
            "metrics": metrics,
        }


def _commit(checkout: Path) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - git with a path the operator gave
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()
