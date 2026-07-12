#!/usr/bin/env python3
"""Normalize non-canonical service_category values in promo_offer_master.

Usage:
    python scripts/normalize_service_category.py --dry-run
    python scripts/normalize_service_category.py
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.service_category_lookup import resolve_service_category
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 500
SELECT = "id,service_name,service_category,channel,status"


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client = load_client()
    rows = fetch_active_rows(client)

    planned: List[Dict[str, Any]] = []
    from_values = Counter()
    to_values = Counter()
    unchanged = 0
    skipped_empty = 0

    for row in rows:
        raw = str(row.get("service_category") or "").strip()
        if not raw:
            skipped_empty += 1
            continue
        canonical, method, _ = resolve_service_category(
            str(row.get("service_name") or ""),
            raw,
            min_confidence="low",
        )
        if not canonical or canonical == raw:
            unchanged += 1
            continue
        from_values[raw] += 1
        to_values[canonical] += 1
        planned.append(
            {
                "id": row["id"],
                "service_name": row.get("service_name"),
                "from_category": raw,
                "to_category": canonical,
                "method": method,
            }
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / f"normalize_service_category_{ts}.csv"
    summary_path = OUTPUT_DIR / f"normalize_service_category_{ts}.json"

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["id", "service_name", "from_category", "to_category", "method"],
        )
        writer.writeheader()
        writer.writerows(planned)

    summary = {
        "dry_run": args.dry_run,
        "active_total": len(rows),
        "skipped_empty": skipped_empty,
        "unchanged": unchanged,
        "planned_updates": len(planned),
        "from_category_top": from_values.most_common(30),
        "to_category_top": to_values.most_common(30),
        "csv_path": str(csv_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))

    if args.dry_run:
        print(f"\nDry-run: would normalize {len(planned)} rows")
        return 0

    for item in planned:
        client.update_row(
            "promo_offer_master",
            item["id"],
            {"service_category": item["to_category"]},
        )

    print(f"\nNormalized {len(planned)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
