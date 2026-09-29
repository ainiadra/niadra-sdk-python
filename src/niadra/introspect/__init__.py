"""A type derived from the company's own PostgreSQL schema (the object type spec, section 8), inside the
company's boundary: the catalog is read, never a row, and neither the catalog nor the proposal leaves.

    niadra types derive --dsn postgresql://reader@replica/erp --table public.orders --out order.json
    niadra types derive --check --dsn postgresql://reader@replica/erp --table public.orders \\
        --declaration order.json

`derive()` turns one table's catalog into a proposed type, with the fingerprint of what it read and the review
a person must do before the proposal goes up as a configuration change: triggers are code, and a CHECK that
is not a list of values is not read. `changes()` is the drift report `--check` sends with the fingerprint:
counts, and the names the declaration already holds, never the schema.

`niadra.introspect.postgres` reads the catalog.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from niadra.turns.digest import canonical, digest

__all__ = ["DeriveError", "Derived", "changes", "derive", "fingerprint", "list_check", "normalize", "to_name"]

# niadra-expr's keywords and context names (the object type spec, 5.5): no field takes them.
RESERVED = frozenset(
    {
        *("and", "or", "not", "in", "true", "false", "none", "yes", "no", "unobserved", "known_defect"),
        *("state", "derived_status", "watch_count", "purpose", "config"),
    }
)
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NUMBER = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_NUMERIC = frozenset({"smallint", "integer", "bigint", "numeric", "real", "double precision"})
_IDENTIFIER = re.compile(r'^(?:[a-z_][a-z0-9_$]*|"(?:[^"]|"")+")$')

MAX_FIELDS = 200
MAX_RELATIONS = 20
MAX_KEY = 8
MAX_STATES = 50


class DeriveError(ValueError):
    """A catalog or an option the derivation refuses, named by `code`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Derived:
    """A proposed type (it validates against `object-type.v0`), the fingerprint of the catalog it came from,
    and what a person must review before it is used."""

    type: dict[str, Any]
    fingerprint: str
    review: list[dict[str, Any]]


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-be")


def normalize(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """The catalog in the order the fingerprint hashes (section 8.2): columns and triggers by name, checks
    as text, enumerations by name and foreign keys by their canonical JSON, every order by UTF-16 code
    units."""
    if catalog.get("catalog") != "postgresql":
        raise DeriveError("unknown_catalog", "only a PostgreSQL catalog is read")
    table = catalog.get("table")
    if not isinstance(table, str) or len(_split_top(table, ".")) != 2:
        raise DeriveError("invalid_catalog", "the table is named <schema>.<name>")
    columns = [
        {
            "name": c["name"],
            "type": c["type"],
            "not_null": bool(c["not_null"]),
            **({"enum": c["enum"]} if c.get("enum") else {}),
        }
        for c in catalog.get("columns") or []
    ]
    if not columns:
        raise DeriveError("no_columns", "the table has no columns")
    foreign_keys = [
        {
            "columns": list(f["columns"]),
            "references": f["references"],
            "referenced_columns": list(f["referenced_columns"]),
        }
        for f in catalog.get("foreign_keys") or []
    ]
    return {
        "catalog": "postgresql",
        "table": table,
        "columns": sorted(columns, key=lambda c: _utf16(c["name"])),
        "primary_key": list(catalog.get("primary_key") or []),
        "checks": sorted((str(d) for d in catalog.get("checks") or []), key=_utf16),
        "enums": sorted(
            ({"name": e["name"], "labels": list(e["labels"])} for e in catalog.get("enums") or []),
            key=lambda e: _utf16(e["name"]),
        ),
        "foreign_keys": sorted(foreign_keys, key=lambda f: _utf16(canonical(f).decode())),
        "triggers": sorted(
            (
                {"name": t["name"], "definition": t["definition"], "enabled": bool(t["enabled"])}
                for t in catalog.get("triggers") or []
            ),
            key=lambda t: _utf16(t["name"]),
        ),
    }


def fingerprint(catalog: Mapping[str, Any]) -> str:
    """`sha256:<hex>` over the canonical JSON of the normalized catalog (section 8.3)."""
    return digest(normalize(catalog))[0]


def to_name(raw: str, limit: int = 64) -> str:
    """A catalog name as a name of the type (section 8.5): A to Z lowercased, every other code point outside
    `[a-z0-9_]` one `_`, `c_` before a name that does not start with a letter, cut at `limit`, and `_`
    after a reserved word."""
    out = "".join(
        ch.lower() if "A" <= ch <= "Z" else ch if ("a" <= ch <= "z" or "0" <= ch <= "9" or ch == "_") else "_"
        for ch in raw
    )
    if not out or not "a" <= out[0] <= "z":
        out = "c_" + out
    out = out[:limit]
    return out + "_" if out in RESERVED else out


def derive(
    catalog: Mapping[str, Any],
    *,
    type_name: str | None = None,
    system: str | None = None,
    ownership: str = "subject",
) -> Derived:
    """The proposed type of one table (section 8.4). `type_name` defaults to the table's name, `system` to
    `postgresql`, `ownership` to `subject` (or `shared`: an agent's working state is never derived)."""
    normalized = normalize(catalog)
    if ownership not in ("subject", "shared"):
        raise DeriveError("ownership_not_derivable", "a derived type is a subject's or a shared one")
    name = type_name if type_name is not None else to_name(_last(normalized["table"]), 40)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
        raise DeriveError("invalid_name", f"{name!r} is not a type name")
    source = system if system is not None else "postgresql"
    if not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", source):
        raise DeriveError("invalid_name", f"{source!r} is not a system name")

    review: list[dict[str, Any]] = []
    columns = normalized["columns"]
    names = _field_names(c["name"] for c in columns)
    lists: dict[str, list[Any]] = {}
    unread: list[str] = []
    for definition in normalized["checks"]:
        found = list_check(definition)
        if found is not None and found[0] in names:
            lists.setdefault(found[0], found[1])
        else:
            unread.append(definition)
    single_fks = {f["columns"][0]: f for f in normalized["foreign_keys"] if len(f["columns"]) == 1}
    labels = {e["name"]: e["labels"] for e in normalized["enums"]}

    fields: dict[str, dict[str, Any]] = {}
    for column in columns:
        kind = _field_type(column, single_fks, lists)
        if kind is None or len(fields) >= MAX_FIELDS:
            review.append({"kind": "column", "column": column["name"], "type": column["type"]})
            continue
        fields[names[column["name"]]] = {"type": kind}
    for definition in unread:
        review.append({"kind": "check", "definition": definition})

    declared: dict[str, Any] = {
        "type": name,
        "version": "1",
        "ownership": ownership,
        "mirror_of": {
            "system": source,
            "derived_by": "introspection",
            "fingerprint": digest(normalized)[0],
            "drift": "alert",
        },
    }
    key = normalized["primary_key"]
    if key and len(key) <= MAX_KEY and all(names.get(c) in fields for c in key):
        declared["key"] = {"natural": [names[c] for c in key]}
    declared["fields"] = fields

    status = next((c for c in ("status", "state") if c in names and names[c] in fields), None)
    if status is not None:
        column = next(c for c in columns if c["name"] == status)
        values = labels.get(column.get("enum") or "", lists.get(status))
        if values is not None:
            if 0 < len(values) <= MAX_STATES and all(isinstance(v, str) and _NAME.match(v) for v in values):
                declared["states"] = _unique(values)
            else:
                review.append({"kind": "states", "column": status})

    relations: dict[str, dict[str, Any]] = {}
    taken: set[str] = set()
    for fk in normalized["foreign_keys"]:
        field = names.get(fk["columns"][0]) if len(fk["columns"]) == 1 else None
        if field is None or field not in fields or len(relations) >= MAX_RELATIONS:
            review.append({"kind": "foreign_key", "columns": fk["columns"], "references": fk["references"]})
            continue
        role = _unused(_role(field), taken, 64)
        taken.add(role)
        relations[role] = {"type": to_name(_last(fk["references"]), 40), "via": field}
    if relations:
        declared["relations"] = relations
    for trigger in normalized["triggers"]:
        review.append({"kind": "trigger", "name": trigger["name"], "enabled": trigger["enabled"]})
    return Derived(declared, declared["mirror_of"]["fingerprint"], review)


def changes(declared: Mapping[str, Any], live: Mapping[str, Any]) -> dict[str, Any]:
    """What differs between the declaration and the type the live catalog derives (section 8.8): counts,
    and the declared fields the difference touches. Never a new name, a value or a definition."""
    before, after = dict(declared.get("fields") or {}), dict(live.get("fields") or {})
    removed = {n for n in before if n not in after}
    retyped = {n for n in before if n in after and before[n].get("type") != after[n].get("type")}
    states_before, states_after = set(declared.get("states") or ()), set(live.get("states") or ())
    relations_before, relations_after = set(declared.get("relations") or {}), set(live.get("relations") or {})
    touched = removed | retyped
    if states_before != states_after:
        touched |= {next((f for f in ("status", "state_") if f in before), "")} - {""}
    return {
        "fields_added": sum(n not in before for n in after),
        "fields_removed": len(removed),
        "fields_retyped": len(retyped),
        "states_added": len(states_after - states_before),
        "states_removed": len(states_before - states_after),
        "relations_added": len(relations_after - relations_before),
        "relations_removed": len(relations_before - relations_after),
        "key_changed": _natural(declared) != _natural(live),
        "fields": sorted(touched, key=_utf16)[:50],
    }


def list_check(definition: str) -> tuple[str, list[Any]] | None:
    """The column and the values of a CHECK that holds a column to a list, as `pg_get_constraintdef` writes
    it (section 8.6): `CHECK ((status = ANY (ARRAY['open'::text, 'paid'::text])))`, the same with a cast
    of the column and of the array, or `CHECK ((status = 'open'::text))`. Anything else is None."""
    text = definition.strip()
    if not text.startswith("CHECK "):
        return None
    text = text[len("CHECK ") :]
    if text.endswith(" NOT VALID"):
        text = text[: -len(" NOT VALID")]
    text = _unwrap(text)
    parts = _split_top(text, " = ")
    if len(parts) != 2:
        return None
    column = _column(parts[0])
    if column is None:
        return None
    right = parts[1]
    if right.startswith("ANY (") and right.endswith(")"):
        values = _array(right[len("ANY (") : -1])
    else:
        single = _literal(right)
        values = None if single is None else [single]
    return None if values is None else (column, values)


def _last(qualified: str) -> str:
    """The name after the schema, unquoted."""
    parts = _split_top(qualified, ".")
    return _unquote(parts[-1])


def _field_names(columns: Iterable[str]) -> dict[str, str]:
    """Each column's field name: `to_name`, with `_2`, `_3` ... after a name an earlier column took."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for column in columns:
        name = _unused(to_name(column), taken, 64)
        taken.add(name)
        out[column] = name
    return out


def _unused(name: str, taken: set[str], limit: int) -> str:
    if name not in taken:
        return name
    n = 2
    while True:
        suffix = f"_{n}"
        candidate = name[: limit - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


def _role(field: str) -> str:
    role = field[: -len("_id")] if field.endswith("_id") else field
    return role if _NAME.match(role) and role not in RESERVED else field


def _field_type(
    column: Mapping[str, Any], fks: Mapping[str, Any], lists: Mapping[str, list[Any]]
) -> str | None:
    """The field type of a column (section 8.6), or None when the catalog type has none."""
    if column["name"] in fks:
        return "ref"
    if column.get("enum") or column["name"] in lists:
        return "enum"
    kind: str = column["type"]
    if kind.endswith("[]"):
        return "list"
    base = kind.split("(", 1)[0].strip()
    if base in ("text", "character varying", "character", "citext", "uuid"):
        return "string"
    if base in _NUMERIC:
        return "number"
    if base == "money":
        return "money"
    if base == "boolean":
        return "bool"
    if base == "date":
        return "date"
    if base.startswith("timestamp"):
        return "datetime"
    if base == "interval":
        return "duration"
    return None


def _natural(declared: Mapping[str, Any]) -> list[str]:
    return list((declared.get("key") or {}).get("natural") or [])


def _unique(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _scan(text: str) -> list[int]:
    """The depth of parentheses and brackets at each character, outside string literals and quoted names
    (-1 inside them)."""
    depth, quote, out = 0, "", []
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(-1)
            if ch == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    out.append(-1)
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            out.append(-1)
        elif ch in "([":
            out.append(depth)
            depth += 1
        elif ch in ")]":
            depth -= 1
            out.append(depth)
        else:
            out.append(depth)
        i += 1
    return out


def _unwrap(text: str) -> str:
    """`text` without the parentheses that enclose all of it."""
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depths = _scan(text)
        if any(d == 0 for d in depths[1:-1]):
            return text
        text = text[1:-1].strip()
    return text


def _split_top(text: str, separator: str) -> list[str]:
    """`text` split at `separator` outside parentheses, literals and quoted names."""
    depths = _scan(text)
    parts, start, i = [], 0, 0
    while i <= len(text) - len(separator):
        if depths[i] == 0 and text.startswith(separator, i):
            parts.append(text[start:i].strip())
            i += len(separator)
            start = i
            continue
        i += 1
    parts.append(text[start:].strip())
    return parts


def _cast(text: str) -> tuple[str, str | None]:
    """`text` without its outermost trailing `::type` casts, and the last type it was cast to."""
    text = _unwrap(text)
    cast = None
    while True:
        parts = _split_top(text, "::")
        if len(parts) < 2:
            return text, cast
        cast = parts[-1]
        text = _unwrap("::".join(parts[:-1]))


def _column(text: str) -> str | None:
    bare, _ = _cast(text)
    return _unquote(bare) if _IDENTIFIER.match(bare) else None


def _unquote(name: str) -> str:
    return name[1:-1].replace('""', '"') if name.startswith('"') and name.endswith('"') else name


def _array(text: str) -> list[Any] | None:
    bare, _ = _cast(text)
    if not (bare.startswith("ARRAY[") and bare.endswith("]")):
        return None
    inner = bare[len("ARRAY[") : -1]
    if not inner.strip():
        return None
    values = []
    for item in _split_top(inner, ","):
        value = _literal(item)
        if value is None:
            return None
        values.append(value)
    return values


def _literal(text: str) -> Any:
    """A constant of a list: a string literal, or a number (PostgreSQL writes a negative one as a string
    cast to a number type)."""
    bare, cast = _cast(text)
    base = (cast or "").split("(", 1)[0].strip()
    if len(bare) >= 2 and bare.startswith("'") and bare.endswith("'"):
        value = bare[1:-1]
        if "'" in value.replace("''", ""):
            return None
        value = value.replace("''", "'")
        return _number(value) if base in _NUMERIC and _NUMBER.match(value) else value
    return _number(bare) if _NUMBER.match(bare) else None


def _number(text: str) -> int | float:
    return int(text) if "." not in text else float(text)
