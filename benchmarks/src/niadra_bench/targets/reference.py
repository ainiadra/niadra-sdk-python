"""The two references every accuracy chart carries: no memory at all, and the whole raw history pasted
into the prompt. They bound what a memory system can change, and they are the two sides of the
validity rule."""

from __future__ import annotations

from niadra_bench.dataset.model import Case
from niadra_bench.identity import Identities
from niadra_bench.targets.base import Retrieved, Target
from niadra_bench.text import render_history


class NoMemory(Target):
    system = "no_memory"

    async def seed(self, case: Case, ids: Identities) -> None:  # noqa: ARG002
        return None

    async def retrieve(self, case: Case, ids: Identities, *, view: str | None = None) -> Retrieved:  # noqa: ARG002
        return Retrieved(text="", elapsed_ms=0.0)


class FullHistory(Target):
    system = "full_history"

    async def seed(self, case: Case, ids: Identities) -> None:  # noqa: ARG002
        return None

    async def retrieve(self, case: Case, ids: Identities, *, view: str | None = None) -> Retrieved:  # noqa: ARG002
        text = "\n".join(ids.fill(line, case.customer.name) for line in render_history(case).splitlines())
        return Retrieved(text=text, elapsed_ms=0.0)
