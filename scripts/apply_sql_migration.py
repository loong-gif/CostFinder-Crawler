#!/usr/bin/env python3
"""Apply a SQL migration file to Supabase Postgres.

Requires SUPABASE_DB_URL (postgresql://...) in environment or .env.
Falls back to printing instructions when no DB URL is configured.

Usage:
    python scripts/apply_sql_migration.py config/sql/promo_membership_plans.sql
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEDGER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL,
    preflight_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    approval_token_hash TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    error TEXT
)
"""


def _check_drop_gate_rest() -> str | None:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not key:
        return None
    try:
        import requests

        response = requests.get(
            f"{base_url.rstrip('/')}/rest/v1/promo_offer_master",
            params={
                "select": "id",
                "status": "eq.active",
                "membership_name": "not.is.null",
                "membership_plan_id": "is.null",
            },
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Prefer": "count=exact",
            },
            timeout=60,
        )
        response.raise_for_status()
        content_range = response.headers.get("content-range", "")
        count = int(content_range.split("/")[-1]) if "/" in content_range else len(response.json())
    except Exception as exc:
        return f"Drop gate REST check failed: {exc}"
    if count:
        return (
            f"Refusing DROP: {count} active offers still have membership_name without membership_plan_id. "
            "Run scripts/archive/migrate_offer_membership_fk.py first."
        )
    return None


def _check_drop_gate(db_url: str) -> str | None:
    """Block DROP until active offers no longer depend on legacy membership_name without FK."""
    try:
        import psycopg
    except ImportError:
        return None
    query = """
        SELECT COUNT(*) FROM promo_offer_master
        WHERE status = 'active'
          AND membership_name IS NOT NULL
          AND membership_plan_id IS NULL
    """
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                count = int(cur.fetchone()[0])
    except Exception as exc:
        return f"Drop gate check failed: {exc}"
    if count:
        return (
            f"Refusing DROP: {count} active offers still have membership_name without membership_plan_id. "
            "Run scripts/archive/migrate_offer_membership_fk.py first."
        )
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply SQL migration file to Supabase Postgres")
    parser.add_argument("sql_file", help="Path to .sql file")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL only, do not execute")
    return parser.parse_args()


def _migration_id_for_path(sql_path: Path) -> str:
    resolved = sql_path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _migration_checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _migration_preflight_json(sql_path: Path, sql: str, checksum: str) -> str:
    payload = {
        "migration_id": _migration_id_for_path(sql_path),
        "sql_file": sql_path.name,
        "checksum": checksum,
        "byte_length": len(sql.encode("utf-8")),
        "line_count": sql.count("\n") + 1,
        "drop_gate_required": "drop_membership_columns" in sql_path.name,
    }
    return json.dumps(payload, sort_keys=True)


def _advisory_lock_key(migration_id: str) -> int:
    digest = hashlib.sha256(migration_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _ensure_ledger_schema(cur) -> None:
    cur.execute(LEDGER_TABLE_SQL)


def _fetch_ledger_row(cur, migration_id: str):
    cur.execute(
        "SELECT migration_id, checksum, status FROM schema_migrations WHERE migration_id = %s",
        (migration_id,),
    )
    return cur.fetchone()


def _upsert_preflight_row(cur, migration_id: str, checksum: str, preflight_json: str) -> None:
    cur.execute(
        """
        INSERT INTO schema_migrations (
            migration_id,
            checksum,
            status,
            preflight_json,
            started_at,
            completed_at,
            error
        ) VALUES (%s, %s, 'preflight', %s::jsonb, CURRENT_TIMESTAMP, NULL, NULL)
        ON CONFLICT (migration_id) DO UPDATE SET
            checksum = EXCLUDED.checksum,
            status = EXCLUDED.status,
            preflight_json = EXCLUDED.preflight_json,
            started_at = EXCLUDED.started_at,
            completed_at = NULL,
            error = NULL
        """,
        (migration_id, checksum, preflight_json),
    )


def _mark_applied(cur, migration_id: str) -> None:
    cur.execute(
        """
        UPDATE schema_migrations
           SET status = 'applied',
               completed_at = CURRENT_TIMESTAMP,
               error = NULL
         WHERE migration_id = %s
        """,
        (migration_id,),
    )


def _mark_failed(cur, migration_id: str, checksum: str, preflight_json: str, error: str) -> None:
    cur.execute(
        """
        UPDATE schema_migrations
           SET checksum = %s,
               status = 'failed',
               preflight_json = %s::jsonb,
               completed_at = CURRENT_TIMESTAMP,
               error = %s
         WHERE migration_id = %s
        """,
        (checksum, preflight_json, error, migration_id),
    )


def _apply_sql_migration(sql_path: Path, sql: str, db_url: str, psycopg) -> int:
    migration_id = _migration_id_for_path(sql_path)
    checksum = _migration_checksum(sql)
    preflight_json = _migration_preflight_json(sql_path, sql, checksum)

    ledger_conn = psycopg.connect(db_url)
    try:
        with ledger_conn.cursor() as cur:
            _ensure_ledger_schema(cur)
            cur.execute("SELECT pg_advisory_lock(%s)", (_advisory_lock_key(migration_id),))
            existing = _fetch_ledger_row(cur, migration_id)
            if existing:
                existing_checksum = existing[1]
                existing_status = existing[2]
                if existing_checksum != checksum:
                    print(
                        "Migration checksum mismatch for "
                        f"{sql_path}: ledger has {existing_checksum}, file has {checksum}",
                        file=sys.stderr,
                    )
                    return 2
                if existing_status == "applied":
                    print(f"Migration already applied: {sql_path}")
                    return 0
            _upsert_preflight_row(cur, migration_id, checksum, preflight_json)
        ledger_conn.commit()

        if "drop_membership_columns" in sql_path.name:
            gate_error = _check_drop_gate_rest()
            if gate_error:
                with ledger_conn.cursor() as cur:
                    _mark_failed(cur, migration_id, checksum, preflight_json, gate_error)
                ledger_conn.commit()
                print(gate_error, file=sys.stderr)
                return 3

        try:
            migration_conn = psycopg.connect(db_url)
        except Exception as exc:
            failure_error = f"Postgres apply failed: {exc}"
            with ledger_conn.cursor() as cur:
                _mark_failed(cur, migration_id, checksum, preflight_json, failure_error)
            ledger_conn.commit()
            print(
                f"{failure_error}\n"
                f"Run this SQL manually in Supabase SQL Editor:\n\n{sql_path}\n",
                file=sys.stderr,
            )
            return 2

        try:
            with migration_conn.cursor() as cur:
                cur.execute(sql)
            migration_conn.commit()
        except Exception as exc:
            migration_conn.rollback()
            failure_error = f"Postgres apply failed: {exc}"
            with ledger_conn.cursor() as cur:
                _mark_failed(cur, migration_id, checksum, preflight_json, failure_error)
            ledger_conn.commit()
            print(
                f"{failure_error}\n"
                f"Run this SQL manually in Supabase SQL Editor:\n\n{sql_path}\n",
                file=sys.stderr,
            )
            return 2
        finally:
            migration_conn.close()

        with ledger_conn.cursor() as cur:
            _mark_applied(cur, migration_id)
        ledger_conn.commit()
    finally:
        ledger_conn.close()

    print(f"Applied migration: {sql_path}")
    return 0


def main() -> int:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")

    sql_path = Path(args.sql_file)
    if not sql_path.is_absolute():
        sql_path = PROJECT_ROOT / sql_path
    if not sql_path.exists():
        print(f"Missing SQL file: {sql_path}", file=sys.stderr)
        return 1

    sql = sql_path.read_text(encoding="utf-8").strip()
    if not sql:
        print("SQL file is empty", file=sys.stderr)
        return 1

    if args.dry_run:
        print(sql)
        return 0

    db_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL") or os.getenv("DB_URL")
    if not db_url:
        print(
            "No SUPABASE_DB_URL/DATABASE_URL configured.\n"
            f"Run this SQL manually in Supabase SQL Editor:\n\n{sql_path}\n",
            file=sys.stderr,
        )
        return 2

    if "drop_membership_columns" in sql_path.name:
        gate_error = _check_drop_gate_rest()
        if gate_error:
            print(gate_error, file=sys.stderr)
            return 3

    try:
        import psycopg
    except ImportError:
        print(
            "Install psycopg to run migrations locally: pip install 'psycopg[binary]'\n"
            f"Or run manually in Supabase SQL Editor: {sql_path}",
            file=sys.stderr,
        )
        return 2

    return _apply_sql_migration(sql_path, sql, db_url, psycopg)


if __name__ == "__main__":
    raise SystemExit(main())
