"""`niadra types derive`: a type proposed from the company's PostgreSQL schema, and `--check` for drift.

    niadra types derive --dsn postgresql://reader@replica/erp --table public.orders --out order.json
    niadra types derive --check --dsn postgresql://reader@replica/erp --table public.orders \\
        --declaration order.json

Both run inside the company's boundary and read the catalog, never a row (`niadra.introspect`). `derive`
writes the proposal (JSON that validates against `object-type.v0`) and lists on stderr what a person must
review before submitting it as a configuration change. `--check` reads the catalog again and compares its
fingerprint with the declaration's; it sends Niadra only the fingerprint and the counts of what changed
(`POST /v1/types/fingerprint`), and says locally, on stderr, what the counts are about. Without
`--declaration` it compares with the type the space serves in its SDK profile, and Niadra decides the drift.

`--check` exits with 0 without drift, 1 on drift and 2 when it could not check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from niadra._transport import Request
from niadra.introspect import DeriveError, changes, derive, fingerprint, to_name

if TYPE_CHECKING:
    from niadra._client import Niadra

DSN_VARIABLE = "NIADRA_DERIVE_DSN"


def add(commands: Any) -> None:
    types = commands.add_parser("types", help="object types derived from the company's schema")
    verbs = types.add_subparsers(dest="verb", required=True)
    derive_ = verbs.add_parser("derive", help="propose a type from a PostgreSQL table, or --check its drift")
    derive_.add_argument("--dsn", help=f"a read-only connection string; defaults to {DSN_VARIABLE}")
    derive_.add_argument("--table", required=True, help="schema.table, or a table in public")
    derive_.add_argument("--type", dest="type_name", help="the type's name; defaults to the table's")
    derive_.add_argument("--system", help="the system the type mirrors; defaults to postgresql")
    derive_.add_argument("--ownership", default="subject", choices=["subject", "shared"])
    derive_.add_argument("--out", type=Path, help="write the proposal here instead of stdout")
    derive_.add_argument("--check", action="store_true", help="compare the live schema with the declaration")
    derive_.add_argument("--declaration", type=Path, help="the declared type, for --check")
    derive_.add_argument("--no-send", action="store_true", help="for --check: tell Niadra nothing")


def run(args: argparse.Namespace, client: Callable[[], Niadra]) -> int:
    from niadra.introspect.postgres import read_catalog

    dsn = args.dsn or os.environ.get(DSN_VARIABLE)
    if not dsn:
        return _fail(f"a connection string is needed: --dsn or {DSN_VARIABLE}")
    try:
        catalog = read_catalog(dsn, args.table)
        if args.check:
            return _check(args, catalog, client)
        derived = derive(catalog, type_name=args.type_name, system=args.system, ownership=args.ownership)
    except DeriveError as error:
        return _fail(f"{error.code}: {error}")
    except ImportError as error:
        return _fail(str(error))
    except Exception as error:  # the driver's own: its message may carry the connection string
        return _fail(f"could not read the catalog ({type(error).__name__})")
    text = json.dumps(derived.type, indent=2, ensure_ascii=False) + "\n"
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for item in derived.review:
        print(f"review: {_review(item)}", file=sys.stderr)
    return 0


def _check(args: argparse.Namespace, catalog: Mapping[str, Any], client: Callable[[], Niadra]) -> int:
    live_fingerprint = fingerprint(catalog)
    niadra: Niadra | None = None
    declared: dict[str, Any] | None = None
    served = args.declaration is None
    if not served:
        declared = json.loads(args.declaration.read_text(encoding="utf-8"))
    elif args.no_send:
        return _fail("--no-send needs --declaration: there is nothing else to compare with")
    else:
        niadra = client()
        declared = _served(niadra, args.type_name or to_name(_table_name(catalog), 40))
        if declared is None:
            return _fail("the space serves no such type: pass --type, or --declaration")
    mirror = declared.get("mirror_of") or {}
    live = derive(
        catalog,
        type_name=declared["type"],
        system=mirror.get("system") or args.system,
        ownership=declared.get("ownership", "subject"),
    ).type
    if served:
        live.pop("relations", None)  # the profile's summary carries no relations: nothing to compare
    report = changes(declared, live)
    drift: bool | None = live_fingerprint != mirror["fingerprint"] if mirror.get("fingerprint") else None
    issue_id = None
    if not args.no_send:
        body = {"type": declared["type"], "fingerprint": live_fingerprint, "changes": report}
        try:
            niadra = niadra or client()
            answer = niadra._transport.request(Request("POST", "/v1/types/fingerprint", json=body))
            drift = bool(answer.get("drift")) if drift is None else drift
            issue_id = answer.get("issue_id")
        except Exception as error:
            print(f"niadra: the fingerprint was not sent ({type(error).__name__})", file=sys.stderr)
    if drift is None:
        return _fail("the declaration has no fingerprint and Niadra did not answer")
    for line in _details(declared, live):
        print(line, file=sys.stderr)
    out = {"type": declared["type"], "drift": drift, "fingerprint": live_fingerprint, "changes": report}
    if issue_id is not None:
        out["issue_id"] = issue_id
    print(json.dumps(out, indent=2))
    return 1 if drift else 0


def _served(niadra: Niadra, name: str) -> dict[str, Any] | None:
    profile = niadra._transport.request(Request("GET", "/v1/sdk/profile"))
    return next((dict(t) for t in (profile or {}).get("types", []) if t.get("type") == name), None)


def _table_name(catalog: Mapping[str, Any]) -> str:
    table = str(catalog["table"]).rsplit(".", 1)[-1]
    return table[1:-1].replace('""', '"') if table.startswith('"') else table


def _details(declared: Mapping[str, Any], live: Mapping[str, Any]) -> list[str]:
    """What the counts are about, named: for the company's own log, never sent."""
    before, after = dict(declared.get("fields") or {}), dict(live.get("fields") or {})
    lines = [f"field added: {n} ({after[n]['type']})" for n in after if n not in before]
    lines += [f"field removed: {n}" for n in before if n not in after]
    lines += [
        f"field retyped: {n} ({before[n].get('type')} to {after[n].get('type')})"
        for n in before
        if n in after and before[n].get("type") != after[n].get("type")
    ]
    states_before, states_after = list(declared.get("states") or ()), list(live.get("states") or ())
    lines += [f"state added: {s}" for s in states_after if s not in states_before]
    lines += [f"state removed: {s}" for s in states_before if s not in states_after]
    relations_before = dict(declared.get("relations") or {})
    relations_after = dict(live.get("relations") or {})
    lines += [f"relation added: {r}" for r in relations_after if r not in relations_before]
    lines += [f"relation removed: {r}" for r in relations_before if r not in relations_after]
    key_before = (declared.get("key") or {}).get("natural") or []
    key_after = (live.get("key") or {}).get("natural") or []
    if key_before != key_after:
        lines.append(f"key: {key_before} to {key_after}")
    return lines


def _review(item: Mapping[str, Any]) -> str:
    kind = item["kind"]
    if kind == "column":
        return f"column {item['column']} ({item['type']}) has no field type: declare it by hand, or leave it"
    if kind == "check":
        return f"CHECK not read as a list of values: {item['definition']}"
    if kind == "states":
        return f"the values of {item['column']} are not state names; declare the states by hand"
    if kind == "foreign_key":
        return f"foreign key {', '.join(item['columns'])} to {item['references']} is not a relation"
    state = "enabled" if item["enabled"] else "disabled"
    return f"trigger {item['name']} ({state}) is code: declare the transitions it enforces in lifecycle"


def _fail(message: str) -> int:
    print(f"niadra: {message}", file=sys.stderr)
    return 2
