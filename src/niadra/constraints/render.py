"""The constraints block rendered for one tool call (`spec/constraints.md`, sections 6 and 7): what the call's
arguments can carry through the tool's binding, what they cannot (the residual), and, after the call, how much
of what was sent the results honored. The server runs the same rules for `POST /v1/constraints` with a tool.

- A hard constraint renders through the binding argument of its field: `in` and `eq` to the argument,
  `not_in` and `ne` to its negation parameter, a comparison or `between` only to an argument that declares
  it in `ops`. One value renders as itself, several as a list; `transform` changes the case of text.
- A hard constraint that lost a conflict of the block is not rendered at all.
- A constraint of a category holds only for a call of that category.
- An attribute renders through the argument of its family (`size` for `size.pants`) when it applies:
  `always`, or `when_asked` and the person asked for it this turn.
- What no argument can express is residual: the SDK filters it from the results when the tool overfetches,
  and it is otherwise unenforced. `exclude` is always residual.
- Advisory mode (the default) changes nothing and suggests. Apply mode adds only what the call left out, and
  only a hard constraint said in this turn or this session, or an attribute the person said; it never
  overrides an argument the call set (the current utterance wins, and the clash is reported as a conflict),
  and never adds an inferred size.
- A block for another beneficiary than the call's does not apply at all.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from niadra.models.signals import ConstraintsBlock, HardConstraint

SAID = frozenset({"stated", "tool_args", "correction"})
_NEGATED = frozenset({"not_in", "ne"})
_POSITIVE = frozenset({"in", "eq"})


@dataclass(frozen=True, slots=True)
class BindingArg:
    """One argument of a tool's binding (`tool-bindings`): the field it carries and how."""

    attr: str
    param: str
    transform: str | None = None
    negation: str | None = None
    ops: tuple[str, ...] = ()
    family: str | None = None
    """The attribute family of the field (`size`), from the type registry."""


@dataclass(frozen=True, slots=True)
class Binding:
    tool: str
    args: tuple[BindingArg, ...]
    overfetch: bool = False


@dataclass(frozen=True, slots=True)
class Call:
    args: Mapping[str, Any]
    for_: str = "self"
    category: str | None = None
    asked: frozenset[str] = frozenset()
    """The attributes the person asked to use this turn ("in my size": `size`)."""


@dataclass(frozen=True, slots=True)
class Rendering:
    applies: bool
    args: Mapping[str, Any]
    """What the call sends: its own arguments, plus what apply mode added."""
    suggested: Mapping[str, Any] = field(default_factory=dict)
    injected: tuple[str, ...] = ()
    hard_sent: tuple[str, ...] = ()
    residual: tuple[str, ...] = ()
    post_filter: tuple[str, ...] = ()
    conflicts: tuple[tuple[str, str], ...] = ()
    """The id of a hard constraint and the argument the call set against it."""


@dataclass(frozen=True, slots=True)
class Honored:
    """What the results show of the hard constraints sent: "sent, verifiable, violated", never only sent."""

    results_checked: int
    violations: int
    unverifiable: int
    """Results without the constrained field: without this count, conformance lies upward."""


def render(
    block: ConstraintsBlock, binding: Binding, call: Call, mode: Literal["advisory", "apply"] = "advisory"
) -> Rendering:
    """The block for one call of the tool `binding` describes."""
    if block.subject.for_ != call.for_:
        return Rendering(applies=False, args=dict(call.args))
    lost = {i for c in block.conflicts for i in c.ids if i != c.kept}
    suggested: dict[str, Any] = {}
    sources: dict[str, list[str]] = {}
    residual: list[str] = []
    conflicts: list[tuple[str, str]] = []
    routed: list[tuple[HardConstraint, str]] = []
    for h in block.hard:
        if h.id in lost or (h.category is not None and h.category != call.category):
            continue
        route = _route(h, binding)
        if route is None:
            residual.append(h.id)
            continue
        arg, param = route
        value = _shape(h.values, arg.transform)
        if param in suggested:
            if h.op not in _POSITIVE | _NEGATED:
                residual.append(h.id)
                continue
            before = _as_list(suggested[param])
            merged = before + [v for v in _as_list(value) if v not in before]
            value = merged[0] if len(merged) == 1 else merged
        suggested[param] = value
        sources.setdefault(param, []).append(h.id)
        routed.append((h, param))
        if _against(h, arg, call.args):
            conflicts.append((h.id, arg.param))
    for a in block.attributes:
        family = a.name.split(".")[0]
        if a.category is not None and a.category != call.category:
            continue
        if a.apply != "always" and family not in call.asked and a.name not in call.asked:
            continue
        carrier = next((b for b in binding.args if b.family == family), None)
        if carrier is not None and carrier.param not in suggested:
            suggested[carrier.param] = _shape([a.value], carrier.transform)
            sources[carrier.param] = [a.id]
    if block.exclude:
        residual.append("exclude")
    args = dict(call.args)
    injected: list[str] = []
    if mode == "apply":
        allowed = {h.id for h in block.hard if h.source in SAID and h.scope in ("turn", "session")}
        allowed -= {i for i, _ in conflicts}
        allowed |= {a.id for a in block.attributes if a.source in ("stated", "correction")}
        for param, value in suggested.items():
            ids = sources[param]
            if param not in args and all(i in allowed for i in ids):
                args[param] = value
                injected += ids
    hard_sent = tuple(h.id for h, param in routed if _sent(h, param, args))
    post_filter = tuple(residual) if binding.overfetch else ()
    return Rendering(
        True, args, suggested, tuple(injected), hard_sent, tuple(residual), post_filter, tuple(conflicts)
    )


def honored(block: ConstraintsBlock, sent: Sequence[str], results: Sequence[Mapping[str, Any]]) -> Honored:
    """Each result item, keyed by `type.field`, is a violation when it breaks a hard constraint that was sent,
    and unverifiable when it breaks none but lacks the field of one."""
    checks = [h for h in block.hard if h.id in sent]
    violations = unverifiable = 0
    for item in results:
        if any(item.get(h.attr) is not None and not satisfies(h.op, h.values, item[h.attr]) for h in checks):
            violations += 1
        elif any(item.get(h.attr) is None for h in checks):
            unverifiable += 1
    return Honored(len(results), violations, unverifiable)


def satisfies(op: str, values: Sequence[Any], value: Any) -> bool:
    """Whether `value` honors `op` over `values`; text is compared folded, numbers as numbers."""
    got, want = _key(value), [_key(v) for v in values]
    try:
        match op:
            case "in" | "eq":
                return got in want
            case "not_in" | "ne":
                return got not in want
            case "lt":
                return bool(got < want[0])
            case "lte":
                return bool(got <= want[0])
            case "gt":
                return bool(got > want[0])
            case "gte":
                return bool(got >= want[0])
            case "between":
                return bool(want[0] <= got <= want[1])
    except TypeError:
        return False
    raise ValueError(f"unknown operator {op!r}")


def _shape(values: Sequence[Any], transform: str | None) -> Any:
    out = [v.lower() if transform == "lower" and isinstance(v, str) else v for v in values]
    out = [v.upper() if transform == "upper" and isinstance(v, str) else v for v in out]
    return out[0] if len(out) == 1 else out


def _key(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return _fold(value).strip()
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    return value


def _fold(text: str) -> str:
    """Lower case without accents, one character for each character, as the claim contract folds text: a
    character that folds to more than one (`ß`) becomes `?`."""
    if text.isascii():
        return text.lower()
    out = []
    for ch in text:
        plain = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)).lower()
        out.append(plain if len(plain) == 1 else "?")
    return "".join(out)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else [value]


def _route(h: HardConstraint, binding: Binding) -> tuple[BindingArg, str] | None:
    """The argument and parameter a hard constraint renders through, if any can express it."""
    for arg in binding.args:
        if arg.attr != h.attr:
            continue
        if h.op in _POSITIVE and (not arg.ops or h.op in arg.ops or "in" in arg.ops):
            return arg, arg.param
        if h.op in _NEGATED and arg.negation is not None:
            return arg, arg.negation
        if h.op not in _POSITIVE | _NEGATED and h.op in arg.ops:
            return arg, arg.param
    return None


def _sent(h: HardConstraint, param: str, args: Mapping[str, Any]) -> bool:
    if param not in args:
        return False
    given = _as_list(args[param])
    if h.op in _NEGATED:
        return all(satisfies("in", given, v) for v in h.values)
    if h.op in _POSITIVE:
        return all(satisfies("in", h.values, v) for v in given)
    return all(satisfies(h.op, h.values, v) for v in given)


def _against(h: HardConstraint, arg: BindingArg, args: Mapping[str, Any]) -> bool:
    """The call set the field's own argument to something the constraint refuses."""
    if arg.param not in args:
        return False
    return any(not satisfies(h.op, h.values, v) for v in _as_list(args[arg.param]))
