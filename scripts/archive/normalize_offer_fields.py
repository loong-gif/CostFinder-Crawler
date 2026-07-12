#!/usr/bin/env python3
"""Normalize unit_type, service_area, and bool fields on active promo_offer_master.

Usage:
    python scripts/normalize_offer_fields.py
    python scripts/normalize_offer_fields.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_field_normalize import normalize_offer_field_values
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 500
SELECT = (
    "id,unit_type,service_area,is_membership_required,is_package,status"
)


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, key)


def fetch_active_rows(client: SupabaseRestClient) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(
            "promo_offer_master",
            SELECT,
            filters={"status": "eq.active"},
            limit=PAGE_SIZE,
            offset=offset,
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def planned_updates(row: Dict[str, Any]) -> Dict[str, Any]:
    subset = {
        key: row.get(key)
        for key in ("unit_type", "service_area", "is_membership_required", "is_package")
        if key in row
    }
    normalized = normalize_offer_field_values(subset)
    updates: Dict[str, Any] = {}
    for key, value in normalized.items():
        if row.get(key) != value:
            updates[key] = value
    return updates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dry_run = not args.apply

    client = load_client()
    rows = fetch_active_rows(client)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "active_rows": len(rows),
        "rows_to_update": 0,
        "updates": [],
    }

    for row in rows:
        updates = planned_updates(row)
        if not updates:
            continue
        report["rows_to_update"] += 1
        report["updates"].append({"id": row["id"], "fields": updates})
        if not dry_run:
            client.update_row(
                "promo_offer_master",
                {"id": f"eq.{row['id']}"},
                updates,
            )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = OUTPUT_DIR / f"normalize_offer_fields_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
