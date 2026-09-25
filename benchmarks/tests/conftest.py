from __future__ import annotations

import pytest

from niadra_bench import config as bench_config
from niadra_bench.dataset import generate
from niadra_bench.dataset.model import Case


@pytest.fixture(scope="session")
def config() -> bench_config.BenchConfig:
    return bench_config.load()


@pytest.fixture(scope="session")
def cases() -> list[Case]:
    return generate.load(bench_config.DATASET_DIR)


def word_tokenizer(text: str) -> int:
    """Tests count words: no tokenizer download."""
    return len(text.split())
