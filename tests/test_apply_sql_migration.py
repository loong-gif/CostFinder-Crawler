from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_sql_migration import (  # noqa: E402
    _apply_sql_migration,
    _migration_checksum,
    _migration_preflight_json,
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self._fetchone = None

    def execute(self, sql, params=None):
        sql_text = sql.strip()
        self.connection.executed.append((sql_text, params))

        if sql_text.startswith("CREATE TABLE IF NOT EXISTS schema_migrations"):
            return

        if sql_text.startswith("SELECT pg_advisory_lock"):
            return

        if sql_text.startswith("SELECT migration_id, checksum, status FROM schema_migrations"):
            self._fetchone = self.connection.ledger_row
            return

        if sql_text.startswith("INSERT INTO schema_migrations"):
            migration_id, checksum, preflight_json = params
            self.connection.ledger_row = (migration_id, checksum, "preflight", preflight_json, None)
            return

        if sql_text.startswith("UPDATE schema_migrations") and "status = 'applied'" in sql_text:
            migration_id = params[0]
            existing = self.connection.ledger_row
            self.connection.ledger_row = (
                migration_id,
                existing[1] if existing else None,
                "applied",
                existing[3] if existing else None,
                None,
            )
            return

        if sql_text.startswith("UPDATE schema_migrations") and "status = 'failed'" in sql_text:
            checksum, preflight_json, error, migration_id = params
            self.connection.ledger_row = (migration_id, checksum, "failed", preflight_json, error)
            return

        if self.connection.fail_execute is not None:
            raise self.connection.fail_execute

        self.connection.migration_sql.append((sql_text, params))

    def fetchone(self):
        return self._fetchone

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, *, ledger_row=None, fail_execute=None):
        self.ledger_row = ledger_row
        self.fail_execute = fail_execute
        self.executed = []
        self.migration_sql = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


class FakePsycopg:
    def __init__(self, connections):
        self.connections = list(connections)
        self.connect_args = []

    def connect(self, db_url):
        self.connect_args.append(db_url)
        if not self.connections:
            raise AssertionError("unexpected connect() call")
        return self.connections.pop(0)


def test_checksum_and_preflight_payload_are_stable():
    sql = "BEGIN;\nSELECT 1;\nCOMMIT;"
    checksum = _migration_checksum(sql)
    preflight = json.loads(_migration_preflight_json(Path("config/sql/m001_test.sql"), sql, checksum))

    assert checksum == _migration_checksum(sql)
    assert preflight["checksum"] == checksum
    assert preflight["sql_file"] == "m001_test.sql"
    assert preflight["drop_gate_required"] is False
    assert preflight["line_count"] == 3


def test_successful_apply_records_preflight_and_applied(tmp_path):
    sql_path = tmp_path / "m001_test.sql"
    sql_path.write_text("SELECT 1;\n", encoding="utf-8")
    ledger_conn = FakeConnection()
    migration_conn = FakeConnection()
    fake_psycopg = FakePsycopg([ledger_conn, migration_conn])

    result = _apply_sql_migration(sql_path, "SELECT 1;\n", "postgres://example", fake_psycopg)

    assert result == 0
    assert ledger_conn.ledger_row[2] == "applied"
    assert ledger_conn.commits == 2
    assert ledger_conn.closed == 1
    assert migration_conn.migration_sql == [("SELECT 1;", None)]
    assert any("pg_advisory_lock" in sql for sql, _ in ledger_conn.executed)


def test_checksum_mismatch_refuses_to_run_migration(tmp_path):
    sql_path = tmp_path / "m001_test.sql"
    sql = "SELECT 1;\n"
    checksum = _migration_checksum(sql)
    ledger_conn = FakeConnection(
        ledger_row=(
            str(sql_path.resolve()),
            "different-checksum",
            "applied",
            "{}",
            None,
        )
    )
    fake_psycopg = FakePsycopg([ledger_conn])

    result = _apply_sql_migration(sql_path, sql, "postgres://example", fake_psycopg)

    assert result == 2
    assert ledger_conn.ledger_row[2] == "applied"
    assert ledger_conn.ledger_row[1] == "different-checksum"
    assert checksum != ledger_conn.ledger_row[1]
    assert len(fake_psycopg.connect_args) == 1


def test_execute_failure_is_recorded_in_ledger(tmp_path):
    sql_path = tmp_path / "m001_test.sql"
    sql = "SELECT 1;"
    ledger_conn = FakeConnection()
    migration_conn = FakeConnection(fail_execute=RuntimeError("boom"))
    fake_psycopg = FakePsycopg([ledger_conn, migration_conn])

    result = _apply_sql_migration(sql_path, sql, "postgres://example", fake_psycopg)

    assert result == 2
    assert ledger_conn.ledger_row[2] == "failed"
    assert "boom" in ledger_conn.ledger_row[4]
    assert migration_conn.rollbacks == 1
    assert ledger_conn.commits == 2


def test_drop_gate_failure_records_failed_status(monkeypatch, tmp_path):
    sql_path = tmp_path / "m004_drop_membership_columns.sql"
    sql = "DROP TABLE example;\n"
    ledger_conn = FakeConnection()
    fake_psycopg = FakePsycopg([ledger_conn])
    monkeypatch.setattr("scripts.apply_sql_migration._check_drop_gate_rest", lambda: "gate blocked")

    result = _apply_sql_migration(sql_path, sql, "postgres://example", fake_psycopg)

    assert result == 3
    assert ledger_conn.ledger_row[2] == "failed"
    assert ledger_conn.ledger_row[4] == "gate blocked"
    assert ledger_conn.commits == 2

