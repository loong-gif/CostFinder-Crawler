#!/usr/bin/env python3
"""Backfill normalized prices on active promo_offer_master rows.

Usage:
    python scripts/backfill_offer_prices.py
    python scripts/backfill_offer_prices.py --apply
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

from utils.offer_price_normalize import normalize_offer_prices
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 500
SELECT = (
    "id,service_name,offer_raw_text,regular_price,discount_price,"
    "discount_amount,discount_percent,status"
)
PRICE_FIELDS = (
    "regular_price",
    "discount_price",
    "discount_amount",
    "discount_percent",
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


def needs_update(row: Dict[str, Any], normalized: Dict[str, Any]) -> Dict[str, Any]:
    updates: Dict[str, Any] = {}
    for field in PRICE_FIELDS:
        new_value = normalized.get(field)
        old_value = row.get(field)
        if new_value is None:
            continue
        if old_value is None or float(old_value) != float(new_value):
            updates[field] = new_value
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
        "both_prices_after": 0,
        "swapped_rows": 0,
        "updates": [],
    }

    for row in rows:
        normalized = normalize_offer_prices(
            regular_price=row.get("regular_price"),
            discount_price=row.get("discount_price"),
            discount_amount=row.get("discount_amount"),
            discount_percent=row.get("discount_percent"),
            offer_raw_text=row.get("offer_raw_text"),
        )
        updates = needs_update(row, normalized)
        if not updates:
            continue

        report["rows_to_update"] += 1
        if normalized.get("regular_price") and normalized.get("discount_price"):
            report["both_prices_after"] += 1
        old_reg = row.get("regular_price")
        old_disc = row.get("discount_price")
        if (
            old_reg is not None
            and old_disc is not None
            and float(old_disc) > float(old_reg)
            and updates
        ):
            report["swapped_rows"] += 1

        report["updates"].append({"id": row["id"], "fields": updates})
        if not dry_run:
            client.update_row(
                "promo_offer_master",
                {"id": f"eq.{row['id']}"},
                updates,
            )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = OUTPUT_DIR / f"backfill_offer_prices_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
