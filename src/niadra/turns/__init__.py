"""Turn records in the agent's process: what an agent read, called, showed, claimed and decided in one turn.

```python
with conversation.turn(build=niadra.build(prompts={"core": "v16"}, model=MODEL)):
    context = conversation.context()
    reply = run_agent(context)  # tools decorated with `@niadra.tool` record themselves
    conversation.agent(reply)
```

Capture copies at the moment and never delays the agent (`niadra.turns.capture`); a bounded queue keeps the
closed turns (`niadra.turns.queue`) and a sender posts them to `POST /v1/turns` in the background, in the
content mode of the space (`niadra.turns.sender`, `niadra.turns.record`), with the values in the company's
own storage in `pointer` mode (`niadra.turns.store`). `niadra.turns.digest` computes the digest every
producer of a record computes the same way, and `niadra.turns.otel` puts the turn on OpenTelemetry spans.
"""

from niadra.turns.capture import CallCapture, TurnFrame, bind, current_call, current_turn
from niadra.turns.recorder import TurnRecorder
from niadra.turns.store import BlobStore, S3Store
from niadra.turns.tool import tool

__all__ = [
    "BlobStore",
    "CallCapture",
    "S3Store",
    "TurnFrame",
    "TurnRecorder",
    "bind",
    "current_call",
    "current_turn",
    "tool",
]
