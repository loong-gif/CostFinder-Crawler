#!/usr/bin/env python3
"""Backfill empty service_category on promo_offer_master.

Usage:
    python scripts/backfill_service_category.py --dry-run
    python scripts/backfill_service_category.py
    python scripts/backfill_service_category.py --min-confidence high
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

from utils.service_category_lookup import (
    build_service_name_category_index,
    resolve_service_category,
)
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 500
SELECT = "id,service_name,service_category,channel,source_url,status"


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
    parser.add_argument(
        "--min-confidence",
        choices=("high", "medium", "low"),
        default="medium",
        help="Minimum inference confidence to apply (default: medium)",
    )
    args = parser.parse_args()

    client = load_client()
    rows = fetch_active_rows(client)
    sibling_index = build_service_name_category_index(rows)

    empty = [r for r in rows if not str(r.get("service_category") or "").strip()]
    channel_counts = Counter(str(r.get("channel") or "") for r in empty)

    planned: List[Dict[str, Any]] = []
    methods = Counter()
    unresolved = Counter()

    for row in empty:
        category, method, confidence = resolve_service_category(
            str(row.get("service_name") or ""),
            row.get("service_category"),
            sibling_index=sibling_index,
            min_confidence=args.min_confidence,
        )
        if not category:
            unresolved[str(row.get("service_name") or "")] += 1
            continue
        methods[method] += 1
        planned.append(
            {
                "id": row["id"],
                "service_name": row.get("service_name"),
                "channel": row.get("channel"),
                "service_category": category,
                "method": method,
                "confidence": confidence,
            }
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / f"backfill_service_category_{ts}.csv"
    summary_path = OUTPUT_DIR / f"backfill_service_category_{ts}.json"

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "id",
                "service_name",
                "channel",
                "service_category",
                "method",
                "confidence",
            ],
        )
        writer.writeheader()
        writer.writerows(planned)

    summary = {
        "dry_run": args.dry_run,
        "min_confidence": args.min_confidence,
        "active_total": len(rows),
        "empty_category": len(empty),
        "planned_updates": len(planned),
        "unresolved": len(empty) - len(planned),
        "by_channel_empty": dict(channel_counts),
        "by_method": dict(methods),
        "top_unresolved_service_names": unresolved.most_common(25),
        "csv_path": str(csv_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))

    if args.dry_run:
        print(f"\nDry-run: would update {len(planned)} rows")
        return 0

    updated = 0
    for item in planned:
        client.update_row(
            "promo_offer_master",
            item["id"],
            {"service_category": item["service_category"]},
        )
        updated += 1

    print(f"\nUpdated {updated} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
