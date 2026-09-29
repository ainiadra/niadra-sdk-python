"""`@tool`: records each call of a function tool in the turn it runs in, whatever the framework.

```python
@niadra.tool("quote", provenance=lambda r: [{"ref": f"health_quote:op:{r['id']}", "fields": r["prices"]}])
async def quote(plan: str, lives: int) -> dict: ...
```

Inside a turn, a call records its arguments when it starts, its result (or its failure) when it ends, its
latency, and the call it was made in. `provenance` turns a result into the objects it showed, each
`{ref, fields, provenance}`: without provenance an observation is for display only. `ui` gives the form of
the result the interface got, when it differs from the one the model saw. Outside a turn, the tool runs
untouched and nothing is recorded. The recording never fails the tool: a provenance function that raises
marks the turn incomplete, and the tool's own result and exceptions pass through unchanged.

A generator tool is recorded piece by piece, and its result is the list of pieces once it is consumed.

With `binding` (the tool's binding, as `niadra.constraints.binding` reads it), a call in a turn that read the
constraints block records what it did with the block: the hard constraints its arguments sent, and over the
objects its result shows, how many were checked, broke one, or lacked the field (the constraints spec, 7). It
is what the day's conformance counts and what the tool counterfactual starts from. Nothing is changed.

In a replay (`niadra.replay`) the call answers from the record when its arguments match a recorded call of the
tool; otherwise it runs only when `dry_run=True` says running it again is safe, and answers `None` (a
divergence) when not.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from niadra.constraints.binding import flag, items, parse
from niadra.constraints.render import Call, honored, render
from niadra.models.turns import TurnObservation
from niadra.turns.capture import CallCapture, TurnFrame, current_turn

logger = logging.getLogger("niadra")

F = TypeVar("F", bound=Callable[..., Any])
Provenance = Callable[[Any], "Iterable[Mapping[str, Any]] | Mapping[str, Any] | None"]


def tool(
    name: str | None = None,
    *,
    provenance: Provenance | None = None,
    ui: Callable[[Any], Any] | None = None,
    exclude: Iterable[str] = (),
    dry_run: bool = False,
    binding: Mapping[str, Any] | None = None,
) -> Callable[[F], F]:
    """Records the decorated tool's calls in the current turn. `name` defaults to the function's name;
    `exclude` names parameters left out of the recorded arguments (a framework's context object);
    `dry_run=True` says a replay may run it again for real when the record has no answer; `binding` is the
    tool's binding, to measure the constraints block against its calls."""

    def decorate(fn: F) -> F:
        signature = inspect.signature(fn)
        left_out = {"self", "cls", *exclude}
        called = name or fn.__name__

        def mark(wrapper: Any) -> F:
            wrapper.__niadra_tool__ = Recorded(called, dry_run, provenance, binding)
            return wrapper  # type: ignore[no-any-return]

        def begin(args: tuple[Any, ...], kwargs: dict[str, Any]) -> CallCapture | None:
            frame = current_turn()
            if frame is None or frame.closed:
                return None
            try:
                bound = signature.bind_partial(*args, **kwargs).arguments
                arguments = {k: v for k, v in bound.items() if k not in left_out}
            except TypeError:
                arguments = {"args": list(args), **kwargs}
            call = frame.adopt(called, arguments) or frame.tool_call(called, arguments)
            if binding is not None and frame.constraints is not None:
                call.measure = _measure(frame, binding, arguments)
            if frame.playback is not None:
                call.played = frame.playback.answer(called, arguments, dry_run=dry_run)
            return call

        def end(call: CallCapture, result: Any) -> None:
            observations = _observations(call, provenance, result)
            shown = None
            if ui is not None:
                try:
                    shown = ui(result)
                except Exception:
                    logger.warning("niadra: the ui form of %s's result failed", called, exc_info=True)
                    call.frame.incomplete()
            call.result(result, ui=shown, observations=observations)

        # A generator runs in its consumer's context between pieces: it is recorded, never made the call in
        # progress, or the consumer's own calls would name it as their parent.
        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def agen(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                call = begin(args, kwargs)
                if call is None:
                    async for item in fn(*args, **kwargs):
                        yield item
                    return
                if call.played is not None and not call.played.live:
                    end(call, call.played.value)
                    for item in call.played.value or ():
                        yield item
                    return
                pieces: list[Any] = []
                try:
                    async for item in fn(*args, **kwargs):
                        pieces.append(item)
                        yield item
                except GeneratorExit:
                    end(call, pieces)
                    raise
                except BaseException as exc:
                    call.failed(exc)
                    raise
                end(call, pieces)

            return mark(agen)

        if inspect.isgeneratorfunction(fn):

            @functools.wraps(fn)
            def gen(*args: Any, **kwargs: Any) -> Iterator[Any]:
                call = begin(args, kwargs)
                if call is None:
                    yield from fn(*args, **kwargs)
                    return
                if call.played is not None and not call.played.live:
                    end(call, call.played.value)
                    yield from call.played.value or ()
                    return
                pieces: list[Any] = []
                try:
                    for item in fn(*args, **kwargs):
                        pieces.append(item)
                        yield item
                except GeneratorExit:
                    end(call, pieces)
                    raise
                except BaseException as exc:
                    call.failed(exc)
                    raise
                end(call, pieces)

            return mark(gen)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                call = begin(args, kwargs)
                if call is None:
                    return await fn(*args, **kwargs)
                with call:
                    if call.played is not None and not call.played.live:
                        end(call, call.played.value)
                        return call.played.value
                    result = await fn(*args, **kwargs)
                    end(call, result)
                    return result

            return mark(run_async)

        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            call = begin(args, kwargs)
            if call is None:
                return fn(*args, **kwargs)
            with call:
                if call.played is not None and not call.played.live:
                    end(call, call.played.value)
                    return call.played.value
                result = fn(*args, **kwargs)
                end(call, result)
                return result

        return mark(run)

    return decorate


@dataclass(frozen=True)
class Recorded:
    """What `@niadra.tool` knows of the function it wraps, for the runners that call it again."""

    name: str
    dry_run: bool
    provenance: Provenance | None
    binding: Mapping[str, Any] | None


def recorded(fn: Any) -> Recorded | None:
    """The `@niadra.tool` of `fn`, if it is one."""
    found = getattr(fn, "__niadra_tool__", None)
    return found if isinstance(found, Recorded) else None


def _measure(
    frame: TurnFrame, raw: Mapping[str, Any], arguments: Mapping[str, Any]
) -> Callable[[Any], dict[str, Any] | None] | None:
    """How the call's result honored the block its arguments were rendered against; None when the block does
    not apply to the call."""
    block = frame.constraints
    assert block is not None
    families = frame._recorder.families() if frame._recorder is not None else {}
    rendering = render(block, parse(raw, families), Call(arguments))
    if not rendering.applies:
        return None

    def measure(result: Any) -> dict[str, Any] | None:
        seen = honored(block, rendering.hard_sent, items(raw, result))
        applied: dict[str, Any] = {
            "constraints": block.version,
            "hard_sent": list(rendering.hard_sent),
            "results_checked": seen.results_checked,
            "violations": seen.violations,
            "unverifiable": seen.unverifiable,
        }
        if flag(raw, result):
            applied["relaxed"] = "declared"
        return applied

    return measure


def _observations(call: CallCapture, provenance: Provenance | None, result: Any) -> list[dict[str, Any]]:
    """What the company's provenance function says the result showed, checked; never raises."""
    if provenance is None:
        return []
    try:
        found = provenance(result)
        items = [found] if isinstance(found, Mapping) else list(found or ())
        return [
            TurnObservation.model_validate(item).model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in items
        ]
    except Exception:
        logger.warning("niadra: the provenance of a %s result failed", call.entry.get("name"), exc_info=True)
        call.frame.incomplete()
        return []
