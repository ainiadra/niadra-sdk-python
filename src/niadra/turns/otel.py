"""The turn on OpenTelemetry spans, without touching the agent's code (Niadra OpenTelemetry conventions, 6).

```python
from opentelemetry.sdk.trace import TracerProvider
from niadra.turns.otel import NiadraBaggageSpanProcessor

provider = TracerProvider()
provider.add_span_processor(NiadraBaggageSpanProcessor())
```

`NiadraBaggageSpanProcessor` sets `niadra.turn.id` and `niadra.pack.hash` on every span that starts inside a
turn: from the Niadra turn open in the task or thread that started the span, and otherwise from the
OpenTelemetry baggage, where a caller in another process put them. Spans the agent's instrumentation emits
(a model call, a tool call, an HTTP request) then carry the turn, and the memory joins them to it.

`turn_baggage()` puts the same two values in the baggage of a context, for a call to another process that
must carry the turn. Baggage travels in the headers of every outgoing call, third parties included, so it
carries only these two values, never anything that names a person. Requires `opentelemetry-api`, and the
processor `opentelemetry-sdk`.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import baggage
from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from niadra.turns.capture import TurnFrame, current_turn

TURN_ID = "niadra.turn.id"
PACK_HASH = "niadra.pack.hash"


def _of(frame: TurnFrame) -> dict[str, str]:
    values = {TURN_ID: frame.turn_id}
    pack = frame.pins.get("niadra", {}).get("pack_hash")
    if pack:
        values[PACK_HASH] = str(pack)
    return values


class NiadraBaggageSpanProcessor(SpanProcessor):
    """Copies the turn's id and its pack's hash onto each span that starts inside the turn."""

    def on_start(self, span: Span, parent_context: otel_context.Context | None = None) -> None:
        values: dict[str, str] = {}
        for key in (TURN_ID, PACK_HASH):
            carried = baggage.get_baggage(key, parent_context)
            if carried is not None:
                values[key] = str(carried)
        # A turn open in this process is the span's own: it wins over one another process passed along.
        frame = current_turn()
        if frame is not None:
            values.update(_of(frame))
        for key, value in values.items():
            span.set_attribute(key, value)

    def on_end(self, span: ReadableSpan) -> None:  # noqa: ARG002 - nothing to do when a span ends
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002 - it holds nothing
        return True


def turn_baggage(frame: TurnFrame | None = None, context: Any = None) -> otel_context.Context:
    """`context` (by default the current one) with the turn's id and pack hash as baggage: attach it
    (`opentelemetry.context.attach`) around a call to another process that should carry the turn."""
    frame = frame or current_turn()
    ctx: otel_context.Context = context if context is not None else otel_context.get_current()
    if frame is None:
        return ctx
    for key, value in _of(frame).items():
        ctx = baggage.set_baggage(key, value, context=ctx)
    return ctx
