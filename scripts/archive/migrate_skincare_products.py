#!/usr/bin/env python3
"""Migrate skincare/retail catalog rows from promo_offer_master to promo_products_master.

Usage:
    python scripts/migrate_skincare_products.py --dry-run
    python scripts/migrate_skincare_products.py
"""
from __future__ import annotations

import argparse
import csv
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

from utils.membership_plans import end_offer_ids
from utils.offer_scope_filter import is_skincare_product_offer
from utils.skincare_products import (
    build_skincare_product_insert_row,
    find_existing_product_id,
)
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 500
STAGING_TABLE = "promo_website_staging"
SELECT_VARIANTS = (
    "id,source_url,source_name,service_name,offer_raw_text,offer_content,discount_price,original_price,regular_price,business_id,status",
    "id,source_url,source_name,service_name,offer_raw_text,offer_content,discount_price,business_id,status",
)


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, key)


def table_exists(client: SupabaseRestClient) -> bool:
    try:
        client.fetch_rows("promo_products_master", "product_id", limit=1)
        return True
    except Exception:
        return False


def fetch_targets(client: SupabaseRestClient) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch: List[Dict[str, Any]] = []
        for select in SELECT_VARIANTS:
            try:
                batch = client.fetch_rows(
                    "promo_offer_master",
                    select,
                    filters={"status": "eq.active"},
                    limit=PAGE_SIZE,
                    offset=offset,
                    order="id.asc",
                )
                break
            except Exception:
                continue
        else:
            raise RuntimeError("Unable to fetch promo_offer_master rows")
        if not batch:
            break
        rows.extend(row for row in batch if is_skincare_product_offer(row))
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def fetch_staging(client: SupabaseRestClient, source_url: str) -> Dict[str, Any] | None:
    url = str(source_url or "").strip().rstrip("/")
    if not url:
        return None
    for candidate in (url, f"{url}/"):
        try:
            rows = client.fetch_rows(
                STAGING_TABLE,
                "promo_website_id,subpage_url,domain_name,business_id",
                filters={"subpage_url": f"eq.{candidate}"},
                limit=1,
            )
        except Exception:
            rows = []
        if rows:
            return rows[0]
    return None


def migrate_offer(client: SupabaseRestClient, offer: Dict[str, Any], *, dry_run: bool) -> str:
    staging = fetch_staging(client, str(offer.get("source_url") or ""))
    product_name = build_skincare_product_insert_row(offer, staging)["product_name"]
    existing = find_existing_product_id(client, str(offer.get("source_url") or ""), product_name)
    if existing is not None:
        end_offer_ids(client, [str(offer["id"])], dry_run=dry_run)
        return "linked_existing"
    row = build_skincare_product_insert_row(offer, staging)
    if dry_run:
        end_offer_ids(client, [str(offer["id"])], dry_run=True)
        return "inserted"
    inserted = client.insert_rows("promo_products_master", [row])
    if not inserted:
        return "failed"
    end_offer_ids(client, [str(offer["id"])], dry_run=False)
    return "inserted"


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate skincare products out of promo_offer_master")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()

    client = load_client()
    if not table_exists(client):
        print(
            "promo_products_master table missing. Run config/sql/promo_products_master.sql first.",
            file=sys.stderr,
        )
        return 2

    offers = fetch_targets(client)
    inserted = linked = failed = ended = 0
    failed_rows: List[Dict[str, Any]] = []

    for offer in offers:
        action = migrate_offer(client, offer, dry_run=args.dry_run)
        if action == "inserted":
            inserted += 1
            ended += 1
        elif action == "linked_existing":
            linked += 1
            ended += 1
        else:
            failed += 1
            failed_rows.append(offer)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "candidates": len(offers),
        "inserted": inserted,
        "linked_existing": linked,
        "ended": ended,
        "failed": failed,
    }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report = out_dir / f"migrate_skincare_products_{stamp}.json"
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if failed_rows:
        csv_path = out_dir / f"migrate_skincare_products_failed_{stamp}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["id", "source_url", "service_name", "offer_raw_text"])
            writer.writeheader()
            for row in failed_rows:
                writer.writerow(
                    {
                        "id": row.get("id"),
                        "source_url": row.get("source_url"),
                        "service_name": row.get("service_name"),
                        "offer_raw_text": row.get("offer_raw_text"),
                    }
                )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
