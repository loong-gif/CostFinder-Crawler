#!/usr/bin/env python3
"""
Insert previously-extracted offers from dry-run JSON into promo_offer_master.
Usage: python scripts/insert_batch_offers.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.supabase_rest import SupabaseRestClient

load_dotenv()

DRY_RUN_JSON = PROJECT_ROOT / "output" / "results" / "batch_extraction_20260708_160224.json"
OFFER_TABLE = "promo_offer_master"
STAGING_TABLE = "promo_website_staging"
BATCH_SIZE = 50

def main():
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    db = SupabaseRestClient(supabase_url, supabase_key)

    data = json.loads(DRY_RUN_JSON.read_text(encoding="utf-8"))
    offers = data.get("offers", [])
    clean_rows_info = data.get("clean_rows", len(offers))
    print(f"Loaded {len(offers)} offers from {DRY_RUN_JSON.name}")

    # Insert in batches
    print(f"Inserting into {OFFER_TABLE}...")
    inserted = 0
    for i in range(0, len(offers), BATCH_SIZE):
        batch = offers[i:i + BATCH_SIZE]
        # Map to existing DB column names
        mapped_batch = []
        for o in batch:
            row = {
                "channel": "Website",
                "source_url": o.get("source_url", ""),
                "source_name": o.get("source_name", ""),
                "template_type": o.get("offer_type") or "",  # offer_type -> template_type
                "service_category": o.get("service_category"),
                "service_name": o.get("service_name"),
                "offer_raw_text": (o.get("offer_raw_text") or "")[:2000],
                "regular_price": o.get("regular_price"),
                "discount_price": o.get("discount_price"),
                "discount_percent": o.get("discount_percent"),
                "discount_amount": o.get("discount_amount"),
                "unit_type": o.get("unit_type"),
                "is_package": "TRUE" if o.get("is_package") == "true" else "FALSE",
                "is_membership_required": "TRUE" if o.get("offer_type") == "membership" else "FALSE",
                "eligibility": o.get("eligibility"),
                "service_area": o.get("service_area"),
                "start_date": o.get("start_date"),
                "end_date": o.get("end_date"),
                "business_id": o.get("business_id") or None,  # must be null or valid FK
                "delivered_unit": o.get("delivered_unit"),
                "min_unit": o.get("min_unit"),
                "membership_plan_id": o.get("membership_plan_id"),
                "offer_content": o.get("offer_content"),
                "status": "active",
            }
            # Remove None values so the DB uses its defaults
            cleaned = {k: v for k, v in row.items() if v is not None}
            mapped_batch.append(cleaned)
        try:
            db.insert_rows(OFFER_TABLE, mapped_batch)
            inserted += len(mapped_batch)
            print(f"  Inserted {inserted}/{len(offers)}")
        except Exception as e:
            err_text = str(e)
            # Try inserting one by one to find the bad rows
            print(f"  Batch insert error at offset {i}, retrying individually...", file=sys.stderr)
            for j, r in enumerate(mapped_batch):
                try:
                    db.insert_rows(OFFER_TABLE, [r])
                    inserted += 1
                except Exception as e2:
                    print(f"    Row {i+j} ({r.get('source_url','?')}) failed: {e2}", file=sys.stderr)
            print(f"  Inserted {inserted}/{len(offers)}")

    if inserted:
        print(f"\nDone. Inserted {inserted} offers.")
    else:
        print("\nNo offers inserted.")
        return

    # Mark processed rows that had successful offers
    print("Marking processed staging rows...")
    # Build set of source_urls
    source_urls = set()
    for o in offers:
        url = o.get("source_url", "")
        if url:
            source_urls.add(url)

    marked = 0
    for url in source_urls:
        try:
            db.update_row(
                STAGING_TABLE,
                {"subpage_url": f"eq.{url}"},
                {"processed_status": True}
            )
            marked += 1
        except Exception as e:
            print(f"  Failed to mark {url}: {e}", file=sys.stderr)
    print(f"Marked {marked} staging rows as processed.")


if __name__ == "__main__":
    main()
