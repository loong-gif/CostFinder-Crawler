#!/usr/bin/env python3
"""Post-migration checks for M024 agent defense patterns."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CHECKS = [
    (
        "authenticated statement_timeout",
        "SELECT rolconfig FROM pg_roles WHERE rolname = 'authenticated'",
        lambda rows: any("statement_timeout=30s" in (row.get("rolconfig") or []) for row in rows),
    ),
    (
        "monitor_event_id unique index",
        "SELECT indexname FROM pg_indexes WHERE indexname = 'uq_promo_offer_change_events_monitor'",
        lambda rows: len(rows) >= 1,
    ),
    (
        "active_promo_offers view",
        "SELECT viewname FROM pg_views WHERE schemaname = 'public' AND viewname = 'active_promo_offers'",
        lambda rows: len(rows) >= 1,
    ),
    (
        "active_businesses view",
        "SELECT viewname FROM pg_views WHERE schemaname = 'public' AND viewname = 'active_businesses'",
        lambda rows: len(rows) >= 1,
    ),
    (
        "pg_stat_statements extension",
        "SELECT extname FROM pg_extension WHERE extname = 'pg_stat_statements'",
        lambda rows: len(rows) >= 1,
    ),
]


def main() -> int:
    db_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL") or os.getenv("DB_URL")
    if not db_url:
        print("SKIP: set SUPABASE_DB_URL to run DB verification")
        return 0

    import psycopg

    failed = 0
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            for name, query, predicate in CHECKS:
                cur.execute(query)
                cols = [desc[0] for desc in cur.description or []]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                ok = predicate(rows)
                print(f"{'OK' if ok else 'FAIL'}: {name}")
                if not ok:
                    failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
