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
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from typing import Any, TypeVar

from niadra.models.turns import TurnObservation
from niadra.turns.capture import CallCapture, current_turn

logger = logging.getLogger("niadra")

F = TypeVar("F", bound=Callable[..., Any])
Provenance = Callable[[Any], "Iterable[Mapping[str, Any]] | Mapping[str, Any] | None"]


def tool(
    name: str | None = None,
    *,
    provenance: Provenance | None = None,
    ui: Callable[[Any], Any] | None = None,
    exclude: Iterable[str] = (),
) -> Callable[[F], F]:
    """Records the decorated tool's calls in the current turn. `name` defaults to the function's name;
    `exclude` names parameters left out of the recorded arguments (a framework's context object)."""

    def decorate(fn: F) -> F:
        signature = inspect.signature(fn)
        left_out = {"self", "cls", *exclude}
        called = name or fn.__name__

        def begin(args: tuple[Any, ...], kwargs: dict[str, Any]) -> CallCapture | None:
            frame = current_turn()
            if frame is None or frame.closed:
                return None
            try:
                bound = signature.bind_partial(*args, **kwargs).arguments
                arguments = {k: v for k, v in bound.items() if k not in left_out}
            except TypeError:
                arguments = {"args": list(args), **kwargs}
            return frame.tool_call(called, arguments)

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

            return agen  # type: ignore[return-value]

        if inspect.isgeneratorfunction(fn):

            @functools.wraps(fn)
            def gen(*args: Any, **kwargs: Any) -> Iterator[Any]:
                call = begin(args, kwargs)
                if call is None:
                    yield from fn(*args, **kwargs)
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

            return gen  # type: ignore[return-value]

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                call = begin(args, kwargs)
                if call is None:
                    return await fn(*args, **kwargs)
                with call:
                    result = await fn(*args, **kwargs)
                    end(call, result)
                    return result

            return run_async  # type: ignore[return-value]

        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            call = begin(args, kwargs)
            if call is None:
                return fn(*args, **kwargs)
            with call:
                result = fn(*args, **kwargs)
                end(call, result)
                return result

        return run  # type: ignore[return-value]

    return decorate


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
