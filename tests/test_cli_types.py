"""`niadra types derive` and `--check` against a real PostgreSQL (`NIADRA_TEST_POSTGRES_DSN`): a synthetic
retail schema is derived, a new value in a CHECK opens drift, and Niadra hears only the fingerprint and the
counts."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from niadra import Niadra
from niadra import cli as command
from niadra.introspect import derive, fingerprint
from niadra_mock import MockApp

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("NIADRA_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="needs NIADRA_TEST_POSTGRES_DSN")

SCHEMA = """
CREATE TYPE {s}.channel AS ENUM ('store', 'site', 'app');
CREATE TABLE {s}.customers (id uuid PRIMARY KEY);
CREATE TABLE {s}.orders (
  id uuid PRIMARY KEY,
  customer_id uuid NOT NULL REFERENCES {s}.customers(id),
  status text NOT NULL CONSTRAINT orders_status CHECK (status IN ('open', 'paid', 'shipped')),
  channel {s}.channel NOT NULL,
  total numeric(12,2) NOT NULL CHECK (total >= 0),
  placed_at timestamptz NOT NULL,
  meta jsonb
);
INSERT INTO {s}.customers VALUES ('0192b1c4-0000-7000-8000-000000000001');
"""


@pytest.fixture
def schema() -> Iterator[str]:
    assert DSN
    name = f"derive_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(SCHEMA.format(s=name))
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {name} CASCADE")


def _sql(statement: str) -> None:
    assert DSN
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(statement)


def _client(app: MockApp) -> Niadra:
    return Niadra(
        "nia_sk_test",
        base_url="http://mock",
        http_client=httpx.Client(transport=httpx.WSGITransport(app=app.wsgi)),
    )


def test_derive_writes_a_type_and_the_review(
    schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "orders.json"
    assert (
        command.main(["types", "derive", "--dsn", str(DSN), "--table", f"{schema}.orders", "--out", str(out)])
        == 0
    )
    declared = json.loads(out.read_text())
    assert declared["type"] == "orders"
    assert declared["mirror_of"]["derived_by"] == "introspection"
    assert declared["states"] == ["open", "paid", "shipped"]
    assert declared["fields"]["channel"] == {"type": "enum"}
    assert declared["relations"] == {"customer": {"type": "customers", "via": "customer_id"}}
    review = capsys.readouterr().err
    assert "column meta (jsonb)" in review
    assert "CHECK ((total >= (0)::numeric))" in review


def test_a_new_value_in_a_check_opens_drift(
    schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    declaration = tmp_path / "orders.json"
    table = ["--dsn", str(DSN), "--table", f"{schema}.orders"]
    assert command.main(["types", "derive", *table, "--out", str(declaration)]) == 0
    check = ["types", "derive", "--check", *table, "--declaration", str(declaration), "--no-send"]
    capsys.readouterr()
    assert command.main(check) == 0
    assert json.loads(capsys.readouterr().out)["drift"] is False

    _sql(f"ALTER TABLE {schema}.orders DROP CONSTRAINT orders_status")
    _sql(
        f"ALTER TABLE {schema}.orders ADD CONSTRAINT orders_status "
        "CHECK (status IN ('open', 'paid', 'shipped', 'returned'))"
    )
    assert command.main(check) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["drift"] is True
    assert report["changes"]["states_added"] == 1
    assert report["changes"]["fields"] == ["status"]
    assert "state added: returned" in captured.err


def test_niadra_hears_only_the_fingerprint_and_the_counts(
    schema: str, tmp_path: Path, mock_app: MockApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_app.cell.features.add("state")
    declaration = tmp_path / "orders.json"
    table = ["--dsn", str(DSN), "--table", f"{schema}.orders"]
    assert command.main(["types", "derive", *table, "--out", str(declaration)]) == 0
    mock_app.cell.agent_features.declare(json.loads(declaration.read_text()))
    sent: list[dict[str, Any]] = []
    handle = mock_app.handle

    def spy(method: str, path: str, query: str, headers: Any, body: bytes, scheme: str = "http") -> Any:
        if path == "/v1/types/fingerprint":
            sent.append(json.loads(body))
        return handle(method, path, query, headers, body, scheme)

    monkeypatch.setattr(mock_app, "handle", spy)
    monkeypatch.setattr(command, "Niadra", lambda: _client(mock_app))
    _sql(f"ALTER TABLE {schema}.orders ADD COLUMN gift boolean")
    assert command.main(["types", "derive", "--check", *table, "--declaration", str(declaration)]) == 1
    assert list(sent[0]) == ["type", "fingerprint", "changes"]
    assert sent[0]["changes"]["fields_added"] == 1
    assert "gift" not in json.dumps(sent[0]), "a new column's name never leaves"
    assert mock_app.cell.agent_features.drift_issues["orders"]["occurrences"] == 1


def test_without_a_declaration_niadra_decides_the_drift(
    schema: str, mock_app: MockApp, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from niadra.introspect.postgres import read_catalog

    assert DSN
    mock_app.cell.features.add("state")
    mock_app.cell.agent_features.declare(derive(read_catalog(DSN, f"{schema}.orders")).type)
    monkeypatch.setattr(command, "Niadra", lambda: _client(mock_app))
    check = ["types", "derive", "--check", "--dsn", DSN, "--table", f"{schema}.orders"]
    assert command.main(check) == 0
    _sql(f"CREATE FUNCTION {schema}.touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
    trigger = f"CREATE TRIGGER touch BEFORE UPDATE ON {schema}.orders FOR EACH ROW"
    _sql(f"{trigger} EXECUTE FUNCTION {schema}.touch()")
    capsys.readouterr()
    assert command.main(check) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issue_id"] == "di_1"
    assert not any(v for k, v in report["changes"].items() if k != "key_changed"), (
        "a trigger the type cannot show"
    )


def test_the_catalog_is_read_without_reading_a_row(schema: str) -> None:
    from psycopg.conninfo import make_conninfo

    from niadra.introspect.postgres import read_catalog

    assert DSN
    role = f"reader_{uuid.uuid4().hex[:8]}"
    _sql(f"CREATE ROLE {role} LOGIN PASSWORD 'catalog-only'")
    _sql(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    try:
        catalog = read_catalog(make_conninfo(DSN, user=role, password="catalog-only"), f"{schema}.orders")
        reader = make_conninfo(DSN, user=role, password="catalog-only")
        with psycopg.connect(reader) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT 1 FROM {schema}.orders")  # noqa: S608 - a schema this test made
        assert fingerprint(catalog) == derive(catalog).fingerprint
    finally:
        _sql(f"DROP OWNED BY {role}")
        _sql(f"DROP ROLE {role}")


def test_a_table_it_cannot_see_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert command.main(["types", "derive", "--dsn", str(DSN), "--table", "nowhere.orders"]) == 2
    assert "table_not_found" in capsys.readouterr().err
