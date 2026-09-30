"""The tool counterfactual (`spec/counterfactual.md`): does one element of the constraints block change what a
tool returns, beyond the tool's own noise? Run in the company's CI, inside its boundary.

```python
from niadra.replay import Counterfactual

runner = Counterfactual(niadra, {"search_products": search_products})
run = runner.run(turn_ids, tool="search_products", element="hard")
print(run.report["effect"], run.report["limits"])
```

For each recorded call of the tool whose arguments or post filter carried the element (the call recorded the
block it `applied`, and the block is in the record), the runner renders the call again without the element and
calls the tool three times at the same moment: the recorded arguments twice (the base, whose two lists measure
the tool's own noise) and the variant once. A tool marked safe to run again (`@niadra.tool(dry_run=True)`, or
`safe=`) runs as it is; any other runs as its dry run (the binding's `capabilities.dry_run_param`), and one
with no dry run is never called (`no_dry_run`). It compares the lists after the post filter (exclusions, when
the binding overfetches) with `overlap_at_k`, finds where the items the person engaged with went, and sends
only positions and overlaps to `POST /v1/measure/counterfactual-runs`, which answers with the report and its
limits.

The records come through the replay case route, so only turns that can be replayed are read. The element is
`constraints` (the whole block), `hard`, `size` (its attributes) or `exclude`. The binding is the one the
space's `tool-bindings` document gives the tool for this source, from the SDK profile; a result's objects are
read with the binding's `results`, or else with the tool's `provenance`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from niadra._transport import Request
from niadra.constraints.binding import items, parse
from niadra.constraints.render import Call, render
from niadra.errors import NiadraError
from niadra.models.signals import ConstraintsBlock
from niadra.replay.playback import BlobError, _materialized
from niadra.replay.runner import _strict
from niadra.turns.record import SDK
from niadra.turns.tool import recorded

if TYPE_CHECKING:
    from niadra._async_client import AsyncNiadra
    from niadra._client import Niadra

MAX_K = 100
MAX_CASES = 5000
PINS = ["prompts", "corpus_digest", "model", "assembler", "tool_schemas"]
Element = Literal["constraints", "hard", "size", "exclude"]


def overlap_at_k(a: Sequence[str], b: Sequence[str], k: int) -> float:
    """Depth-weighted average overlap of `a` and `b` down to `min(k, the longer list's length)`, with
    `w(d) = 1 / log2(d + 1)`. An item repeated within a list counts once, at its first position. Two equal
    lists overlap 1, and so do two empty lists; a list against an empty one overlaps 0."""
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k must be 1 to {MAX_K}")
    first, second = _once(a), _once(b)
    depth = min(k, max(len(first), len(second)))
    if depth == 0:
        return 1.0
    weighted = total = 0.0
    for d in range(1, depth + 1):
        weight = 1 / math.log2(d + 1)
        weighted += weight * len(set(first[:d]) & set(second[:d])) / d
        total += weight
    return weighted / total


@dataclass
class CounterfactualRun:
    """What the run sent and what Niadra answered: `report` is the recorder's (overlap, noise floor, effect,
    the sign test, the engaged items and the limits); `cases` are the cases as sent; `untouched` counts the
    recorded calls the element did not touch, and `unread` the turns whose record could not be read."""

    report: dict[str, Any]
    cases: list[dict[str, Any]] = field(default_factory=list)
    untouched: int = 0
    unread: int = 0


Tool = Callable[..., Any]


class _Runner:
    def __init__(
        self,
        tools: Mapping[str, Tool],
        read: Callable[[str], bytes] | None,
        safe: Iterable[str],
        profile: Callable[[], Any],
    ) -> None:
        self._tools = dict(tools)
        self._read = read
        self._safe = set(safe)
        self._profile = profile

    async def run(
        self,
        send: Callable[[Request], Awaitable[Any]],
        turn_ids: Sequence[str],
        scenario_ids: Sequence[str],
        tool: str,
        element: Element,
        k: int,
        label: str | None,
    ) -> CounterfactualRun:
        if tool not in self._tools:
            raise NiadraError(f"no function for the tool {tool}: pass it in tools")
        if self._served(tool) is None:
            raise NiadraError(f"the space binds no tool {tool} for this key's source: see its tool-bindings")
        if not 1 <= k <= MAX_K:
            raise ValueError(f"k must be 1 to {MAX_K}")
        ids = list(turn_ids)
        if scenario_ids:
            found = await send(
                Request("GET", "/v1/scenarios", params={"ids": ",".join(scenario_ids), "limit": 50})
            )
            ids += [t for s in found.get("items", []) for t in s["turn_ids"] if t not in ids]
        run = CounterfactualRun({})
        for turn_id in ids:
            body = {
                "turn_id": turn_id,
                "mode": "hermetic_turn",
                "build": {"pins": {}, "sdk": SDK},
                "vary": PINS,
            }
            try:
                case = await send(Request("POST", "/v1/replay/cases", json=body))
            except NiadraError:
                run.unread += 1
                continue
            for call in _calls(case["record"], tool):
                result = await self._case(case["record"], call, tool, element, k)
                if result is None:
                    run.untouched += 1
                else:
                    run.cases.append(result)
        if not run.cases:
            raise NiadraError("no recorded call of the tool carried the element: nothing to report")
        report = {"tool": tool, "element": element, "k": k, "cases": run.cases[:MAX_CASES]}
        if label is not None:
            report["label"] = label
        run.report = await send(Request("POST", "/v1/measure/counterfactual-runs", json=report))
        return run

    async def _case(
        self, record: Mapping[str, Any], call: Mapping[str, Any], tool: str, element: Element, k: int
    ) -> dict[str, Any] | None:
        base = {"turn_id": record["turn_id"], "call_id": call["call_id"]}
        fn = self._tools[tool]
        wrapped = recorded(fn)
        raw = self._served(tool)
        assert raw is not None  # `run` refuses a tool the space does not bind
        try:
            block = ConstraintsBlock.model_validate(self._value(record, _block_blob(record, call)))
            args = dict(self._value(record, call.get("args")))
        except (BlobError, ValueError, TypeError):
            return {**base, "status": "infrastructure_error", "dry_run": False}
        families = self._families()
        binding = parse(raw, families)
        asked = frozenset(a.name.split(".")[0] for a in block.attributes)
        variant_block = _without(block, element)
        full = render(block, binding, Call({}, asked=asked))
        without = render(variant_block, binding, Call({}, asked=asked))
        moved = {
            p
            for p in {*full.suggested, *without.suggested}
            if full.suggested.get(p) != without.suggested.get(p)
        }
        carried = any(
            p in full.suggested and _same(args.get(p), full.suggested[p]) for p in moved if p in args
        )
        filters = ("exclude" in full.post_filter, "exclude" in without.post_filter)
        if not carried and not (filters[0] != filters[1] and block.exclude):
            return None
        variant = {k2: v for k2, v in args.items() if k2 not in moved}
        variant.update({p: without.suggested[p] for p in moved if p in without.suggested})
        dry_run = False
        if not ((wrapped is not None and wrapped.dry_run) or tool in self._safe):
            param = (raw.get("capabilities") or {}).get("dry_run_param")
            if not param:
                return {**base, "status": "no_dry_run", "dry_run": False}
            args, variant, dry_run = {**args, param: True}, {**variant, param: True}, True
        try:
            first = await _call(fn, args)
            second = await _call(fn, args)
            third = await _call(fn, variant)
        except Exception:
            return {**base, "status": "tool_error", "dry_run": dry_run}
        try:
            lists = [self._refs(raw, wrapped, r) for r in (first, second, third)]
        except Exception:
            return {**base, "status": "infrastructure_error", "dry_run": dry_run}
        excluded = set(block.exclude)
        base_1, base_2 = ([r for r in lst if r not in excluded] if filters[0] else lst for lst in lists[:2])
        varied = [r for r in lists[2] if r not in excluded] if filters[1] else lists[2]
        presented = _presented(record, call)
        depth = min((presented or {}).get("visible_k") or k, MAX_K)
        return {
            **base,
            "status": "completed",
            "dry_run": dry_run,
            "k": depth,
            "overlap": round(overlap_at_k(base_1, varied, depth), 6),
            "noise": round(overlap_at_k(base_1, base_2, depth), 6),
            "base_count": len(base_1),
            "variant_count": len(varied),
            "engaged": [_positions(ref, base_1, varied) for ref in _engaged(record, presented)][:50],
        }

    def _value(self, record: Mapping[str, Any], key: str | None) -> Any:
        blob = (record.get("blobs") or {}).get(key) if key else None
        if blob is None:
            raise BlobError("the record does not hold the value")
        return _materialized(blob, self._read)

    def _refs(self, raw: Mapping[str, Any], wrapped: Any, result: Any) -> list[str]:
        if raw.get("results"):
            return [str(i["ref"]) for i in items(raw, result) if "ref" in i]
        if wrapped is not None and wrapped.provenance is not None:
            found = wrapped.provenance(result)
            return [str(o["ref"]) for o in ([found] if isinstance(found, Mapping) else list(found or ()))]
        raise BlobError("the tool's result has no objects to compare: give its binding results or provenance")

    def _served(self, tool: str) -> Mapping[str, Any] | None:
        profile = self._profile()
        bindings = profile.tool_bindings if profile is not None else []
        found = next((b for b in bindings if b.tool == tool), None)
        return found.model_dump(mode="json") if found is not None else None

    def _families(self) -> dict[str, str]:
        profile = self._profile()
        types = profile.types if profile is not None else []
        return {
            f"{t['type']}.{name}": spec["attribute"]["family"]
            for t in types
            for name, spec in (t.get("fields") or {}).items()
            if (spec.get("attribute") or {}).get("family")
        }


class Counterfactual:
    """The tool counterfactual with the sync client. `tools` maps each tool's name to the company's function,
    called with the recorded arguments as keywords; `read(pointer)` reads values kept by pointer (by default
    the client's content resolver); `safe` names tools that may run again as they are. Each tool's binding is
    the one the SDK profile serves."""

    def __init__(
        self,
        niadra: Niadra,
        tools: Mapping[str, Tool],
        *,
        read: Callable[[str], bytes] | None = None,
        safe: Iterable[str] = (),
    ) -> None:
        self._niadra = niadra
        reader = read if read is not None else (niadra.content.read if niadra.content.registered else None)
        self._runner = _Runner(tools, _strict(reader), safe, lambda: niadra._profile.profile)

    def run(
        self,
        turn_ids: Sequence[str] = (),
        *,
        tool: str,
        element: Element,
        scenario_ids: Sequence[str] = (),
        k: int = 10,
        label: str | None = None,
    ) -> CounterfactualRun:
        """Runs every recorded call of `tool` in the turns (and the scenarios' turns) the element touched, and
        returns what Niadra reported. Raises when no call carried the element."""
        transport = self._niadra._transport
        self._niadra._refresh_profile()

        async def send(request: Request) -> Any:
            return await asyncio.to_thread(transport.request, request)

        return asyncio.run(self._runner.run(send, turn_ids, scenario_ids, tool, element, k, label))


class AsyncCounterfactual:
    """`Counterfactual` for the async client: `await AsyncCounterfactual(niadra, tools).run(...)`."""

    def __init__(
        self,
        niadra: AsyncNiadra,
        tools: Mapping[str, Tool],
        *,
        read: Callable[[str], bytes] | None = None,
        safe: Iterable[str] = (),
    ) -> None:
        self._niadra = niadra
        reader = read if read is not None else (niadra.content.read if niadra.content.registered else None)
        self._runner = _Runner(tools, _strict(reader), safe, lambda: niadra._profile.profile)

    async def run(
        self,
        turn_ids: Sequence[str] = (),
        *,
        tool: str,
        element: Element,
        scenario_ids: Sequence[str] = (),
        k: int = 10,
        label: str | None = None,
    ) -> CounterfactualRun:
        await self._niadra._refresh_profile()
        return await self._runner.run(
            self._niadra._transport.request, turn_ids, scenario_ids, tool, element, k, label
        )


def _calls(record: Mapping[str, Any], tool: str) -> list[Mapping[str, Any]]:
    """The recorded calls of `tool` that answered and measured a constraints block."""
    return [
        c
        for c in record.get("calls") or []
        if c.get("kind") == "tool"
        and c.get("name") == tool
        and c.get("status", "ok") == "ok"
        and c.get("applied")
    ]


def _block_blob(record: Mapping[str, Any], call: Mapping[str, Any]) -> str | None:
    version = call["applied"].get("constraints")
    read = next(
        (
            r
            for r in record.get("reads") or []
            if r.get("surface") == "constraints" and r.get("version") == version
        ),
        None,
    )
    return read.get("blob") if read is not None else None


def _without(block: ConstraintsBlock, element: Element) -> ConstraintsBlock:
    """The block the variant renders from: the same block without the element."""
    if element == "constraints":
        return block.model_copy(update={"hard": [], "attributes": [], "exclude": [], "conflicts": []})
    if element == "hard":
        return block.model_copy(update={"hard": [], "conflicts": []})
    if element == "size":
        return block.model_copy(update={"attributes": []})
    return block.model_copy(update={"exclude": []})


def _same(given: Any, suggested: Any) -> bool:
    """The call's argument is what the block rendered, as JSON: a list in any order, and one value alone or in
    a list, count as the same."""

    def norm(value: Any) -> list[str]:
        return sorted(json.dumps(v, sort_keys=True) for v in (value if isinstance(value, list) else [value]))

    return bool(norm(given) == norm(suggested))


def _presented(record: Mapping[str, Any], call: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The list the person was shown from the call's result: the presented list with the most of the objects
    the call's result showed, or else the one named after the tool."""
    shown = {o.get("ref") for o in call.get("observations") or []}
    lists = [i for i in record.get("interactions") or [] if i.get("kind") == "presented"]
    best = max(lists, key=lambda p: len(shown & {i.get("ref") for i in p.get("items") or []}), default=None)
    if best is not None and shown & {i.get("ref") for i in best.get("items") or []}:
        return dict(best)
    named = next((p for p in lists if p.get("list_kind") == call.get("name")), None)
    return dict(named) if named is not None else None


def _engaged(record: Mapping[str, Any], presented: Mapping[str, Any] | None) -> list[str]:
    if presented is None:
        return []
    exposure = str(presented.get("exposure_id"))
    return [
        str(i["ref"])
        for i in record.get("interactions") or []
        if i.get("kind") == "engaged" and str(i.get("exposure_id")) == exposure and i.get("ref")
    ]


def _positions(ref: str, base: Sequence[str], variant: Sequence[str]) -> dict[str, int]:
    """The item's 1-based position in each list, over the whole list; absent where the list lacks it."""
    out = {}
    if ref in base:
        out["base"] = list(base).index(ref) + 1
    if ref in variant:
        out["variant"] = list(variant).index(ref) + 1
    return out


async def _call(fn: Tool, args: Mapping[str, Any]) -> Any:
    out = fn(**args)
    return await out if inspect.isawaitable(out) else out


def _once(items_: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items_:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
