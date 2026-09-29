"""Field access at the tool's output (`@niadra.tool(..., mask_output=True)`): a field the calling key may not
read never reaches the model.

The SDK profile says, for each type, the fields this key may not read (`field_access`: `mask` or `deny`).
After the tool returns, the result the model gets loses every `deny` field and has every `mask` field's value
replaced with `[masked]`, wherever the field appears in it, for the types of the objects the result showed
(its `provenance`; every declared type when the tool has none).

When Niadra does not answer, the last profile read keeps applying. With no profile ever read, the result
passes as it is, unless the tool says `on_unknown="block"`: then the model gets only a marker that the result
was withheld, for a company that needs the check to fail closed."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel

MASKED = "[masked]"
WITHHELD = "[withheld: the fields this agent may read are not known yet]"

Access = Callable[[], Mapping[str, Mapping[str, str]] | None]
"""The fields each type hides from this key, by type: None while no profile was ever read."""
OnUnknown = Literal["pass", "block"]


def protect(
    result: Any,
    access: Mapping[str, Mapping[str, str]] | None,
    types: Iterable[str] | None,
    on_unknown: OnUnknown,
) -> Any:
    """`result` as the model may get it."""
    if access is None:
        return WITHHELD if on_unknown == "block" else result
    names = list(access) if types is None else [t for t in types if t in access]
    rules: dict[str, str] = {}
    for name in names:
        for field, effect in access[name].items():
            rules[field] = "deny" if "deny" in (effect, rules.get(field)) else effect
    if not rules:
        return result
    if isinstance(result, BaseModel):
        return _walk(result.model_dump(mode="json", by_alias=True), rules)
    if isinstance(result, str) and result.lstrip()[:1] in ("{", "["):
        try:
            return json.dumps(_walk(json.loads(result), rules))
        except ValueError:
            return result
    return _walk(result, rules)


def _walk(value: Any, rules: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        return {
            k: MASKED if rules.get(k) == "mask" else _walk(v, rules)
            for k, v in value.items()
            if rules.get(k) != "deny"
        }
    if isinstance(value, list | tuple):
        return [_walk(v, rules) for v in value]
    return value
