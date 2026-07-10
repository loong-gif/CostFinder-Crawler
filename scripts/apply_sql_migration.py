#!/usr/bin/env python3
"""Apply a SQL migration file to Supabase Postgres.

Requires SUPABASE_DB_URL (postgresql://...) in environment or .env.
Falls back to printing instructions when no DB URL is configured.

Usage:
    python scripts/apply_sql_migration.py config/sql/promo_membership_plans.sql
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
            "Run scripts/migrate_offer_membership_fk.py first."
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
            "Run scripts/migrate_offer_membership_fk.py first."
        )
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply SQL migration file to Supabase Postgres")
    parser.add_argument("sql_file", help="Path to .sql file")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL only, do not execute")
    return parser.parse_args()


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

    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
    except Exception as exc:
        print(
            f"Postgres apply failed: {exc}\n"
            f"Run this SQL manually in Supabase SQL Editor:\n\n{sql_path}\n",
            file=sys.stderr,
        )
        return 2

    print(f"Applied migration: {sql_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
