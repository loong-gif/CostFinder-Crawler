#!/usr/bin/env python3
"""Read-only Supabase schema preflight for staged CostFinder migrations."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "legacy_base": {
        "promo_offer_master": {"id", "business_id", "status"},
        "promo_website_staging": {"promo_website_id", "business_id", "page_content"},
        "promo_monitor_state": {"monitor_id", "last_check_id"},
        "master_business_info": {"business_id"},
    },
    "m004": {"operation_runs": set(), "notification_outbox": set(), "schema_migrations": set()},
    "m006": {
        "promo_crawl_runs": set(),
        "promo_page_segments": set(),
        "promo_offer_evidence": set(),
        "promo_offer_change_events": set(),
        "promo_offer_match_candidates": set(),
        "promo_offer_status_history": set(),
    },
}


def fetch_schema() -> dict[str, set[str]]:
    env = dotenv_values(ROOT / ".env")
    base = (env.get("SUPABASE_URL") or "").rstrip("/")
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or env.get("SUPABASE_WRITER_KEY")
    if not base or not key:
        raise RuntimeError("SUPABASE_URL and a read-capable Supabase key are required")
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"apikey": key, "Authorization": f"Bearer {key}"})
    result: dict[str, set[str]] = {}
    table_names = sorted({name for group in EXPECTED.values() for name in group})
    for name in table_names:
        response = session.get(
            f"{base}/rest/v1/{name}",
            params={"select": "*", "limit": "1"},
            timeout=30,
        )
        if response.status_code == 404:
            continue
        response.raise_for_status()
        rows = response.json()
        result[name] = set(rows[0]) if rows else set()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    try:
        actual = fetch_schema()
    except Exception as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 2
    report = {}
    for stage, tables in EXPECTED.items():
        report[stage] = {}
        for table, columns in tables.items():
            if table not in actual:
                report[stage][table] = {"state": "missing", "missing_columns": sorted(columns)}
            else:
                missing = sorted(columns - actual[table])
                report[stage][table] = {"state": "ready" if not missing else "column_gap", "missing_columns": missing}
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for stage, tables in report.items():
            print(stage)
            for table, result in tables.items():
                suffix = f" missing={','.join(result['missing_columns'])}" if result["missing_columns"] else ""
                print(f"  {table}: {result['state']}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
