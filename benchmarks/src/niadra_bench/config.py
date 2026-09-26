"""The frozen configuration: `config/benchmark.toml` and `config/mem0.config.json`, and their hash."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# The benchmarks/ directory: config, dataset and results live next to the package source.
ROOT = Path(os.environ.get("BENCH_ROOT") or Path(__file__).resolve().parents[2])
CONFIG_DIR = ROOT / "config"
DATASET_DIR = ROOT / "dataset"
RESULTS_DIR = ROOT / "results"
# Dataset versions: v1 is the dataset of the first published run, with its settings in
# benchmark.toml; v2 adds categories, with its settings in a file of its own, so a v1 run keeps the
# configuration hash of the runs published before v2 existed.
DATASET_VERSIONS = ("v1", "v2")
DEFAULT_DATASET = "v1"
_DATASET_FILES = {"v2": "dataset.v2.toml"}
_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Environment(_Frozen):
    region: str
    # Niadra's cell: the machine and the database that serve it.
    machine_class: str
    database_class: str
    # Where the harness and every other system run (a temporary host of their own since 26/09/2026).
    harness_host: str | None = None


class ProductionSettings(_Frozen):
    """What a run may send to Niadra's production cell (config [production], and its reasons there)."""

    seed_batches_per_s: float = Field(gt=0)
    seed_concurrency: int = Field(ge=1)
    read_concurrency: int = Field(ge=1)
    settle_interval_s: float = Field(ge=0)
    latency_rates: list[int]
    history_rates: list[int]
    ingest_rates: list[int]
    pause_on_overload_s: float = Field(ge=0)


class RunSettings(_Frozen):
    repetitions: int = Field(ge=1)
    concurrency: int = Field(ge=1)
    settle_quiet_s: float = Field(ge=0)
    settle_timeout_s: float = Field(ge=0)


class DatasetSettings(_Frozen):
    seed: int
    cases_per_language: int
    languages: list[str]
    min_sessions: int
    max_sessions: int
    categories: dict[str, int]
    # Dataset v2: the size of a `long_history` customer, CRM profile included.
    long_min_sessions: int | None = None
    long_max_sessions: int | None = None


class LatencySettings(_Frozen):
    rates: list[int]
    duration_s: float
    conversations: int
    request_timeout_s: float


class TokenSettings(_Frozen):
    encoding: str


class HistorySettings(_Frozen):
    rates: list[int]
    duration_s: float
    conversations: int
    request_timeout_s: float
    max_tokens: int


class IngestSettings(_Frozen):
    rates: list[int]
    duration_s: float
    conversations: int
    request_timeout_s: float
    turns_per_conversation: int = Field(ge=1)
    mem0_modes: list[Literal["raw", "infer"]]
    cooldown_s: float = Field(ge=0)


class FreshnessSettings(_Frozen):
    trials: int
    poll_interval_ms: int
    timeout_s: float


class ResilienceSettings(_Frozen):
    trials: int
    delay_ms: int
    status: int
    turn_budget_ms: int


class NiadraSettings(_Frozen):
    views: list[str]


class Mem0Settings(_Frozen):
    top_k: int
    threshold: float
    render_header: str


class ModelCall(_Frozen):
    model: str
    temperature: float
    max_tokens: int


class Models(_Frozen):
    extraction: str
    embedder: str


class NiadraPrices(_Frozen):
    source: str
    low: float
    high: float


class Prices(_Frozen):
    checked_on: str
    openrouter: dict[str, Any]
    niadra: NiadraPrices
    mem0_platform: dict[str, Any]

    def model_price(self, model: str) -> tuple[float, float]:
        """USD per million tokens, input and output."""
        value = self.openrouter.get(model)
        if not isinstance(value, list) or len(value) != 2:
            raise KeyError(f"no price for {model} in [prices.openrouter]")
        return float(value[0]), float(value[1])

    def mem0_plans(self) -> dict[str, tuple[float, int, int]]:
        return {
            name: (float(v[0]), int(v[1]), int(v[2]))
            for name, v in self.mem0_platform.items()
            if name != "source"
        }


class CostSettings(_Frozen):
    turns_per_conversation: int


class BenchConfig(_Frozen):
    environment: Environment
    production: ProductionSettings
    run: RunSettings
    dataset: DatasetSettings
    latency: LatencySettings
    tokens: TokenSettings
    history: HistorySettings
    ingest: IngestSettings
    freshness: FreshnessSettings
    resilience: ResilienceSettings
    niadra: NiadraSettings
    mem0: Mem0Settings
    agent: ModelCall
    judge: ModelCall
    models: Models
    prices: Prices
    cost: CostSettings


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(config_dir: Path = CONFIG_DIR) -> BenchConfig:
    with (config_dir / "benchmark.toml").open("rb") as handle:
        return BenchConfig.model_validate(tomllib.load(handle))


def _check_version(version: str) -> None:
    if version not in DATASET_VERSIONS:
        raise ValueError(f"unknown dataset version {version!r}; choose from {', '.join(DATASET_VERSIONS)}")


def dataset_dir(version: str = DEFAULT_DATASET, root: Path | None = None) -> Path:
    """Where a dataset version is committed: v1 at `dataset/`, later versions under it."""
    _check_version(version)
    base = root or DATASET_DIR
    return base if version == "v1" else base / version


def dataset_settings(
    config: BenchConfig, version: str = DEFAULT_DATASET, config_dir: Path = CONFIG_DIR
) -> DatasetSettings:
    """The generator settings of a dataset version: v1 from benchmark.toml, v2 from its own file."""
    _check_version(version)
    if version not in _DATASET_FILES:
        return config.dataset
    with (config_dir / _DATASET_FILES[version]).open("rb") as handle:
        return DatasetSettings.model_validate(tomllib.load(handle)["dataset"])


def config_hash(config_dir: Path = CONFIG_DIR, dataset: str = DEFAULT_DATASET) -> str:
    """One hash over the frozen files, in a fixed order. A v1 run hashes the two files every run hashed
    before v2 existed, so its hash is unchanged; a v2 run also hashes the v2 dataset settings."""
    _check_version(dataset)
    digest = hashlib.sha256()
    names = ["benchmark.toml", "mem0.config.json"]
    if dataset in _DATASET_FILES:
        names.append(_DATASET_FILES[dataset])
    for name in names:
        digest.update(name.encode())
        digest.update(_sha256(config_dir / name).encode())
    return "sha256:" + digest.hexdigest()


def mem0_config(
    config_dir: Path = CONFIG_DIR, env: dict[str, str] | None = None, *, reranker: bool = False
) -> dict[str, Any]:
    """The Mem0 configuration with `${NAME}` filled from the environment and `_comment` keys dropped."""
    source = dict(os.environ if env is None else env)
    raw = (config_dir / "mem0.config.json").read_text()

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in source:
            raise KeyError(f"{name} is not set; config/mem0.config.json needs it")
        return json.dumps(source[name])[1:-1]

    config: dict[str, Any] = json.loads(_PLACEHOLDER.sub(fill, raw))
    config = {k: v for k, v in config.items() if not k.startswith("_")}
    if not reranker:
        config.pop("reranker", None)
    return config
