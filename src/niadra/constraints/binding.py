"""A tool's binding (`tool-bindings`, as the object type registry writes it): which argument carries which
field, how to read the objects out of a result, and what the tool can do.

```python
SEARCH = {
    "tool": "search_products",
    "args": [{"attr": "item_variant.color", "param": "color", "negation": {"param": "not_color"}}],
    "results": [
        {"path": "ui.cards[*]", "type": "item_variant", "namespace": "store", "id": "variant_id",
         "fields": {"color": "color"}},
    ],
    "capabilities": {"overfetch": False, "relax_flag": "$.meta.relaxed", "dry_run_param": "dry_run"},
}
```

`parse()` gives the renderer its `Binding`; `items()` reads a result's objects keyed `type.field`, with their
`ref` (`type:namespace:id`, the namespace from the result's entry), which is what the SDK measures the hard
constraints sent against (`honored()`) and what the tool counterfactual compares.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from niadra.constraints.render import Binding, BindingArg


def parse(raw: Mapping[str, Any], families: Mapping[str, str] | None = None) -> Binding:
    """The binding the renderer takes. `families` maps a field (`type.field`) to its attribute family, from
    the type registry."""
    args = []
    for arg in raw.get("args") or ():
        negation = arg.get("negation") or {}
        args.append(
            BindingArg(
                arg["attr"],
                arg["param"],
                arg.get("transform"),
                negation.get("param"),
                tuple(arg.get("ops") or ()),
                (families or {}).get(arg["attr"]),
            )
        )
    overfetch = bool(raw.get("overfetch", (raw.get("capabilities") or {}).get("overfetch", False)))
    return Binding(str(raw["tool"]), tuple(args), overfetch)


def items(raw: Mapping[str, Any], result: Any) -> list[dict[str, Any]]:
    """The objects a result shows, in order: each `{ref, "<type>.<field>": value}`. A result the binding does
    not describe shows none."""
    out: list[dict[str, Any]] = []
    for spec in raw.get("results") or ():
        kind = spec.get("type")
        for found in read(result, spec.get("path", "$")):
            if not isinstance(found, Mapping):
                continue
            item: dict[str, Any] = {
                f"{kind}.{field}": found[key]
                for field, key in (spec.get("fields") or {}).items()
                if key in found
            }
            ident = found.get(spec.get("id", "id"))
            if ident is not None:
                item["ref"] = f"{kind}:{spec.get('namespace', 'default')}:{ident}"
            out.append(item)
    return out


def flag(raw: Mapping[str, Any], result: Any) -> bool:
    """Whether the result says the tool relaxed what it was asked (the binding's `relax_flag`)."""
    path = (raw.get("capabilities") or {}).get("relax_flag")
    return isinstance(path, str) and any(v is True for v in read(result, path))


def read(value: Any, path: str) -> list[Any]:
    """What a small path reads out of a JSON value: `$` the value, `a.b` a key, `a[*]` each item of a list."""
    found = [value]
    for part in path.removeprefix("$").strip(".").split(".") if path.strip("$.") else ():
        many = part.endswith("[*]")
        key = part.removesuffix("[*]")
        step: list[Any] = []
        for current in found:
            got = current.get(key) if isinstance(current, Mapping) and key else current if not key else None
            if many:
                step.extend(got if isinstance(got, list) else [])
            elif got is not None:
                step.append(got)
        found = step
    return found
