"""What every memory system under test does for the harness: take a customer's history, and give back
the text an agent would put in its prompt before answering."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from niadra_bench.dataset.model import Case
from niadra_bench.identity import Identities


@dataclass
class Retrieved:
    """The memory block for one probe, as the agent receives it."""

    text: str
    elapsed_ms: float
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Target(ABC):
    #: The system as the results name it: niadra, mem0_oss, mem0_oss_rerank, mem0_platform, ...
    system: str
    #: known_id or per_channel_id for Mem0; None when the scenario does not change what is sent.
    scenario: str | None = None
    #: False for targets that read what another target seeded (the rerank target reads the REST store).
    seeds: bool = True

    async def start(self) -> None:  # noqa: B027 - optional hook
        """Connects and configures. Called once per run."""

    @abstractmethod
    async def seed(self, case: Case, ids: Identities) -> None:
        """Writes the case's history, in chronological order."""

    def seed_report(self) -> dict[str, Any]:
        """What the last seeding observed beyond failures (read once, then cleared)."""
        return {}

    async def settle(self, pairs: list[tuple[Case, Identities]]) -> dict[str, Any]:  # noqa: ARG002
        """Waits until what was seeded can be read. Returns what it observed."""
        return {}

    @abstractmethod
    async def retrieve(self, case: Case, ids: Identities, *, view: str | None = None) -> Retrieved:
        """The memory block for the case's probe question."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Releases clients."""

    def versions(self) -> dict[str, str]:
        return {}

    @property
    def label(self) -> str:
        return f"{self.system}:{self.scenario}" if self.scenario else self.system


class Stopwatch:
    def __init__(self) -> None:
        self.started = time.perf_counter()

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000
