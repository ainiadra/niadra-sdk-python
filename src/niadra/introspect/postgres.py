"""One PostgreSQL table's catalog (the object type spec, 8.1), read in a read-only transaction with
`search_path` set to `pg_catalog` alone, so every name outside it comes schema-qualified whatever the
connection's own path. Only the catalog is read, never a row. Needs `pip install 'niadra[postgres]'`.
"""

from __future__ import annotations

from typing import Any

from niadra.introspect import DeriveError

_TABLE = """
SELECT c.oid, format('%%I.%%I', n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = to_regclass(%s) AND c.relkind IN ('r', 'p')
"""
_COLUMNS = """
SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       CASE WHEN t.typtype = 'e' THEN format('%%I.%%I', tn.nspname, t.typname) END
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
JOIN pg_namespace tn ON tn.oid = t.typnamespace
WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
_CONSTRAINTS = """
SELECT con.contype, pg_get_constraintdef(con.oid),
       ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY AS k(num, ord)
             JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.num ORDER BY k.ord),
       CASE WHEN con.contype = 'f' THEN format('%%I.%%I', fn.nspname, fc.relname) END,
       ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY AS k(num, ord)
             JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.num ORDER BY k.ord)
FROM pg_constraint con
LEFT JOIN pg_class fc ON fc.oid = con.confrelid
LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
WHERE con.conrelid = %s AND con.contype IN ('p', 'c', 'f')
"""
_ENUMS = """
SELECT format('%%I.%%I', n.nspname, t.typname), array_agg(e.enumlabel ORDER BY e.enumsortorder)
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
JOIN pg_enum e ON e.enumtypid = t.oid
WHERE t.oid IN (SELECT a.atttypid FROM pg_attribute a
                WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped)
GROUP BY 1
"""
_TRIGGERS = """
SELECT tgname, pg_get_triggerdef(oid), tgenabled <> 'D'
FROM pg_trigger WHERE tgrelid = %s AND NOT tgisinternal
"""


def read_catalog(dsn: str, table: str) -> dict[str, Any]:
    """The catalog of `table` (`schema.name`, or a name in `public`) as the derivation takes it. Raises
    `DeriveError` (`table_not_found`) for a table the connection cannot see."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("reading a PostgreSQL catalog needs pip install 'niadra[postgres]'") from exc

    qualified = table if "." in table else f"public.{table}"
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        with conn.transaction(force_rollback=True), conn.cursor() as cur:
            cur.execute("SET LOCAL search_path TO pg_catalog")
            cur.execute(_TABLE, (qualified,))
            found = cur.fetchone()
            if found is None:
                raise DeriveError("table_not_found", f"no table {qualified} this connection can read")
            oid, name = found
            cur.execute(_COLUMNS, (oid,))
            columns = [
                {"name": n, "type": t, "not_null": nn, **({"enum": e} if e else {})}
                for n, t, nn, e in cur.fetchall()
            ]
            cur.execute(_CONSTRAINTS, (oid,))
            constraints = cur.fetchall()
            cur.execute(_ENUMS, (oid,))
            enums = [{"name": n, "labels": list(labels)} for n, labels in cur.fetchall()]
            cur.execute(_TRIGGERS, (oid,))
            triggers = [{"name": n, "definition": d, "enabled": e} for n, d, e in cur.fetchall()]
    primary = next((list(cols) for kind, _, cols, _, _ in constraints if kind == "p"), [])
    return {
        "catalog": "postgresql",
        "table": name,
        "columns": columns,
        "primary_key": primary,
        "checks": [definition for kind, definition, _, _, _ in constraints if kind == "c"],
        "enums": enums,
        "foreign_keys": [
            {"columns": list(cols), "references": ref, "referenced_columns": list(refcols)}
            for kind, _, cols, ref, refcols in constraints
            if kind == "f"
        ],
        "triggers": triggers,
    }
