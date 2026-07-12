#!/usr/bin/env python3
"""End consultation, membership-plan, and skincare-product rows in promo_offer_master.

Optionally migrates membership plans / skincare products before ending.

Usage:
    python scripts/cleanup_non_service_offers.py --dry-run
    python scripts/cleanup_non_service_offers.py
    python scripts/cleanup_non_service_offers.py --migrate-membership
    python scripts/cleanup_non_service_offers.py --migrate-skincare
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

from scripts.migrate_membership_offers_to_plans import migrate_offer as migrate_membership_offer
from scripts.migrate_skincare_products import migrate_offer as migrate_skincare_offer
from utils.membership_plans import end_offer_ids
from utils.offer_scope_filter import exclude_reason, should_exclude_from_offer_master
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 500
SELECT = (
    "id,source_url,source_name,service_name,offer_raw_text,offer_content,"
    "discount_price,template_type,membership_plan_id,raw_service_name,status"
)


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, key)


SELECT_VARIANTS = (
    SELECT,
    (
        "id,source_url,source_name,service_name,offer_raw_text,offer_content,"
        "discount_price,template_type,membership_plan_id,status"
    ),
)


def fetch_active_offers(client: SupabaseRestClient) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    select = SELECT_VARIANTS[0]
    print("Fetching active offers...", flush=True)
    while True:
        batch: List[Dict[str, Any]] = []
        for candidate in SELECT_VARIANTS:
            try:
                batch = client.fetch_rows(
                    "promo_offer_master",
                    candidate,
                    filters={"status": "eq.active"},
                    limit=PAGE_SIZE,
                    offset=offset,
                    order="id.asc",
                )
                select = candidate
                break
            except Exception:
                continue
        else:
            raise RuntimeError("Unable to fetch active offers")
        if not batch:
            break
        rows.extend(batch)
        print(f"  fetched {len(rows)} active offers...", flush=True)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="End non-service offers in master")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--migrate-membership",
        action="store_true",
        help="Try promo_membership_plans insert before ending membership-plan rows",
    )
    parser.add_argument(
        "--migrate-skincare",
        action="store_true",
        help="Try promo_products_master insert before ending skincare/retail rows",
    )
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()

    client = load_client()
    offers = fetch_active_offers(client)
    targets = [row for row in offers if should_exclude_from_offer_master(row)]
    print(f"Found {len(targets)} targets (of {len(offers)} active offers)", flush=True)

    ended = 0
    migrated_membership = 0
    migrated_skincare = 0
    by_reason: Dict[str, int] = {"consultation": 0, "membership_plan": 0, "skincare_product": 0}
    actions: List[Dict[str, Any]] = []

    for index, offer in enumerate(targets, start=1):
        reason = exclude_reason(offer)
        by_reason[reason] = by_reason.get(reason, 0) + 1
        action = "ended"
        if reason == "membership_plan" and args.migrate_membership:
            migrate_action = migrate_membership_offer(client, offer, dry_run=args.dry_run)
            if migrate_action in {"inserted", "linked_existing"}:
                migrated_membership += 1
                ended += 1
                actions.append({"id": offer.get("id"), "reason": reason, "action": migrate_action})
                if index % 10 == 0 or index == len(targets):
                    print(f"  progress: {index}/{len(targets)}", flush=True)
                continue
        if reason == "skincare_product" and args.migrate_skincare:
            migrate_action = migrate_skincare_offer(client, offer, dry_run=args.dry_run)
            if migrate_action in {"inserted", "linked_existing"}:
                migrated_skincare += 1
                ended += 1
                actions.append({"id": offer.get("id"), "reason": reason, "action": migrate_action})
                if index % 10 == 0 or index == len(targets):
                    print(f"  progress: {index}/{len(targets)}", flush=True)
                continue
        if not args.dry_run:
            end_offer_ids(client, [str(offer["id"])], dry_run=False)
        else:
            end_offer_ids(client, [str(offer["id"])], dry_run=True)
        ended += 1
        actions.append({"id": offer.get("id"), "reason": reason, "action": action})
        if index % 10 == 0 or index == len(targets):
            print(f"  progress: {index}/{len(targets)}", flush=True)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "migrate_membership": args.migrate_membership,
        "migrate_skincare": args.migrate_skincare,
        "active_scanned": len(offers),
        "targets": len(targets),
        "ended": ended,
        "migrated_membership_plans": migrated_membership,
        "migrated_skincare_products": migrated_skincare,
        "by_reason": by_reason,
    }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report = out_dir / f"cleanup_non_service_offers_{stamp}.json"
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = out_dir / f"cleanup_non_service_offers_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "reason", "action", "service_name", "source_url", "offer_raw_text"],
        )
        writer.writeheader()
        for offer, row in zip(targets, actions):
            writer.writerow(
                {
                    "id": offer.get("id"),
                    "reason": row["reason"],
                    "action": row["action"],
                    "service_name": offer.get("service_name"),
                    "source_url": offer.get("source_url"),
                    "offer_raw_text": str(offer.get("offer_raw_text") or "")[:200],
                }
            )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Done. Report: {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
