"""Replay inside the company's boundary (`spec/replay.md`): Niadra keeps the scenarios and decides the
verdict; the company's CI runs the agent again on recorded turns.

```python
from niadra.replay import Replayer

run = Replayer(niadra, build_agent, build=niadra.build(prompts={"core": "v17"}, model=MODEL)).run(
    ["sc_quote_after_price_change"], runs=5, vary=["prompts"]
)
assert run.verdict != "regression", run.summary
```

For each turn of each scenario the runner asks for the case (`POST /v1/replay/cases`) with the build it
runs, checks the pins itself, fetches every recorded value it needs (by pointer through `niadra.content`, or
`read=`) and checks its digest. It then runs the agent N times: `build_agent()` makes a fresh agent, called
with a `ReplayInput` (the customer's message, the history, the pack of the time). Inside the run, tools
decorated with `@niadra.tool` answer from the record when their arguments match a recorded call; nothing the
agent sends, declares or checks leaves for Niadra, and a read returns the pack of the time. The assertions
are evaluated on the replayed record, which stays here, and the results go to `POST /v1/scenario-runs`, which
answers with the statistical verdict: `pass`, `flaky`, `infrastructure_error`, `pin_mismatch` or
`regression`.

`niadra replay` (`niadra.cli`) runs the same from the command line, for a CI job.
"""

from niadra.replay.runner import AsyncReplayer, Replayer, ReplayInput, ReplayRun

__all__ = ["AsyncReplayer", "ReplayInput", "ReplayRun", "Replayer"]
