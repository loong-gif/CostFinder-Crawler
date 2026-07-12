#!/usr/bin/env python3
"""Migrate mis-filed membership plan offers from promo_offer_master to promo_membership_plans.

Default scope: active rows with service_name = 'Membership'.
Optional: --include-template-type for template_type=membership pure plan rows.

Usage:
    python scripts/migrate_membership_offers_to_plans.py --dry-run
    python scripts/migrate_membership_offers_to_plans.py
    python scripts/migrate_membership_offers_to_plans.py --include-template-type
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.membership_plans import (
    _is_pure_membership_offer,
    _normalize_source_url,
    build_membership_plan_insert_row_from_offer,
    can_migrate_offer_to_plan,
    end_offer_ids,
    find_existing_plan_id,
    offer_row_to_membership_plan,
)
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 500
STAGING_TABLE = "promo_website_staging"
OFFER_SELECT = (
    "id,source_url,source_name,service_name,offer_raw_text,offer_content,"
    "discount_price,membership_price,membership_name,raw_service_name,"
    "template_type,membership_plan_id,business_id,status"
)
OFFER_SELECT_FALLBACK = (
    "id,source_url,source_name,service_name,offer_raw_text,offer_content,"
    "discount_price,membership_name,raw_service_name,"
    "template_type,membership_plan_id,business_id,status"
)


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, key)


OFFER_SELECT_VARIANTS = (
    OFFER_SELECT,
    OFFER_SELECT_FALLBACK,
    (
        "id,source_url,source_name,service_name,offer_raw_text,offer_content,"
        "discount_price,template_type,membership_plan_id,business_id,status"
    ),
)


def fetch_membership_offers(
    client: SupabaseRestClient,
    *,
    include_template_type: bool,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    select = OFFER_SELECT_VARIANTS[0]
    while True:
        batch: List[Dict[str, Any]] = []
        for candidate in OFFER_SELECT_VARIANTS:
            try:
                batch = client.fetch_rows(
                    "promo_offer_master",
                    candidate,
                    filters={"status": "eq.active", "service_name": "eq.Membership"},
                    limit=PAGE_SIZE,
                    offset=offset,
                    order="id.asc",
                )
                select = candidate
                break
            except Exception:
                continue
        else:
            raise RuntimeError("Unable to fetch promo_offer_master membership offers with known select variants")
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if not include_template_type:
        return rows

    seen_ids = {str(row.get("id") or "") for row in rows}
    offset = 0
    while True:
        batch = client.fetch_rows(
            "promo_offer_master",
            select,
            filters={"status": "eq.active", "template_type": "eq.membership"},
            limit=PAGE_SIZE,
            offset=offset,
            order="id.asc",
        )
        if not batch:
            break
        for row in batch:
            row_id = str(row.get("id") or "")
            if row_id in seen_ids:
                continue
            if row.get("membership_plan_id"):
                continue
            if _is_pure_membership_offer(row):
                rows.append(row)
                seen_ids.add(row_id)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def fetch_staging_for_url(client: SupabaseRestClient, source_url: str) -> Optional[Dict[str, Any]]:
    norm_url = _normalize_source_url(source_url)
    if not norm_url:
        return None
    for url in (norm_url, f"{norm_url}/"):
        try:
            rows = client.fetch_rows(
                STAGING_TABLE,
                "promo_website_id,subpage_url,domain_name,business_id,crawl_timestamp",
                filters={"subpage_url": f"eq.{url}"},
                limit=1,
                order="crawl_timestamp.desc",
            )
        except Exception:
            rows = []
        if rows:
            return rows[0]
    return None


def migrate_offer(
    client: SupabaseRestClient,
    offer: Dict[str, Any],
    *,
    dry_run: bool,
) -> str:
    """Return action: inserted | linked_existing | unmigrated."""
    if not can_migrate_offer_to_plan(offer):
        return "unmigrated"

    plan = offer_row_to_membership_plan(offer)
    tier_name = plan["tier_name"]
    source_url = str(offer.get("source_url") or "")
    existing_id = find_existing_plan_id(client, source_url, tier_name)
    if existing_id is not None:
        end_offer_ids(client, [str(offer["id"])], dry_run=dry_run)
        return "linked_existing"

    staging_row = fetch_staging_for_url(client, source_url)
    plan_row = build_membership_plan_insert_row_from_offer(offer, staging_row)
    if dry_run:
        end_offer_ids(client, [str(offer["id"])], dry_run=True)
        return "inserted"

    inserted = client.insert_rows("promo_membership_plans", [plan_row])
    if not inserted:
        return "unmigrated"
    end_offer_ids(client, [str(offer["id"])], dry_run=False)
    return "inserted"


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Membership offers to promo_membership_plans")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-template-type",
        action="store_true",
        help="Also migrate active template_type=membership pure plan rows",
    )
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()

    client = load_client()
    offers = fetch_membership_offers(client, include_template_type=args.include_template_type)

    inserted = 0
    linked_existing = 0
    ended = 0
    unmigrated: List[Dict[str, Any]] = []

    for offer in offers:
        action = migrate_offer(client, offer, dry_run=args.dry_run)
        if action == "inserted":
            inserted += 1
            ended += 1
        elif action == "linked_existing":
            linked_existing += 1
            ended += 1
        else:
            unmigrated.append(offer)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "include_template_type": args.include_template_type,
        "candidates": len(offers),
        "plans_inserted": inserted,
        "linked_existing": linked_existing,
        "offers_ended": ended,
        "unmigrated": len(unmigrated),
    }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report = out_dir / f"migrate_membership_offers_to_plans_{stamp}.json"
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if unmigrated:
        csv_path = out_dir / f"migrate_membership_offers_unmigrated_{stamp}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["id", "source_url", "service_name", "offer_raw_text", "discount_price"],
            )
            writer.writeheader()
            for row in unmigrated:
                writer.writerow(
                    {
                        "id": row.get("id"),
                        "source_url": row.get("source_url"),
                        "service_name": row.get("service_name"),
                        "offer_raw_text": row.get("offer_raw_text"),
                        "discount_price": row.get("discount_price"),
                    }
                )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
