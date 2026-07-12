#!/usr/bin/env python3
"""Migrate legacy membership columns to membership_plan_id FK.

1. End plan-as-offer rows (pure membership fee offers without FK).
2. Backfill membership_plan_id from membership_name + source_url match.

Usage:
    python scripts/migrate_offer_membership_fk.py --dry-run
    python scripts/migrate_offer_membership_fk.py
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.membership_plan_lookup import normalize_plan_name, resolve_plan_fields
from utils.membership_plans import end_offer_ids, find_stale_membership_offer_ids
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 500


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, key)


def fetch_all_active_offers(client: SupabaseRestClient) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    select = "id,source_url,service_name,offer_raw_text,membership_plan_id,membership_name,status,template_type"
    while True:
        try:
            batch = client.fetch_rows(
                "promo_offer_master",
                select,
                filters={"status": "eq.active"},
                limit=PAGE_SIZE,
                offset=offset,
                order="id.asc",
            )
        except Exception:
            select = "id,source_url,service_name,offer_raw_text,membership_plan_id,status,template_type"
            batch = client.fetch_rows(
                "promo_offer_master",
                select,
                filters={"status": "eq.active"},
                limit=PAGE_SIZE,
                offset=offset,
                order="id.asc",
            )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def fetch_all_plans(client: SupabaseRestClient) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(
            "promo_membership_plans",
            "plan_id,source_url,tier_name,plan_name",
            limit=PAGE_SIZE,
            offset=offset,
            order="plan_id.asc",
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def index_plans(plans: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_url: Dict[str, List[Dict[str, Any]]] = {}
    for plan in plans:
        url = str(plan.get("source_url") or "").strip().rstrip("/")
        if not url:
            continue
        by_url.setdefault(url, []).append(plan)
        by_url.setdefault(url + "/", []).append(plan)
    return by_url


def match_plan_id(offer: Dict[str, Any], plans_by_url: Dict[str, List[Dict[str, Any]]]) -> Optional[int]:
    if offer.get("membership_plan_id"):
        return int(offer["membership_plan_id"])
    source_url = str(offer.get("source_url") or "").strip().rstrip("/")
    candidates = plans_by_url.get(source_url) or plans_by_url.get(source_url + "/") or []
    if not candidates:
        return None
    name_raw = str(offer.get("membership_name") or offer.get("service_name") or "")
    name_hints = [normalize_plan_name(part) for part in re.split(r"[/|]", name_raw) if normalize_plan_name(part)]
    if not name_hints:
        return int(candidates[0]["plan_id"]) if len(candidates) == 1 else None
    for name_hint in name_hints:
        for plan in candidates:
            for field in ("tier_name", "plan_name"):
                if normalize_plan_name(plan.get(field)) == name_hint:
                    return int(plan["plan_id"])
            tier_norm = normalize_plan_name(plan.get("tier_name"))
            plan_norm = normalize_plan_name(plan.get("plan_name"))
            if name_hint in tier_norm or name_hint in plan_norm or tier_norm in name_hint or plan_norm in name_hint:
                return int(plan["plan_id"])
    return int(candidates[0]["plan_id"]) if len(candidates) == 1 else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy membership columns to membership_plan_id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()

    client = load_client()
    offers = fetch_all_active_offers(client)
    plans = fetch_all_plans(client)
    plans_by_url = index_plans(plans)

    ended_by_url: Dict[str, List[str]] = {}
    seen_urls: set[str] = set()
    for offer in offers:
        url = str(offer.get("source_url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        stale = find_stale_membership_offer_ids(client, url, exclude_ids=set())
        if stale:
            ended_by_url[url] = stale

    ended_ids = sorted({oid for ids in ended_by_url.values() for oid in ids})
    if not args.dry_run and ended_ids:
        end_offer_ids(client, ended_ids, dry_run=False)

    backfilled = 0
    unmatched: List[Dict[str, Any]] = []
    for offer in offers:
        if str(offer.get("id") or "") in ended_ids:
            continue
        if offer.get("membership_plan_id"):
            continue
        membership_name = str(offer.get("membership_name") or "").strip()
        if not membership_name and not offer.get("is_membership_required"):
            continue
        plan_id = match_plan_id(offer, plans_by_url)
        if plan_id is None:
            unmatched.append(offer)
            continue
        if args.dry_run:
            backfilled += 1
            continue
        client.update_row(
            "promo_offer_master",
            {"id": f"eq.{offer['id']}"},
            {"membership_plan_id": plan_id},
        )
        backfilled += 1

    cleared_names = 0
    for offer in unmatched:
        if str(offer.get("id") or "") in ended_ids:
            continue
        if not str(offer.get("membership_name") or "").strip():
            continue
        if args.dry_run:
            cleared_names += 1
            continue
        client.update_row(
            "promo_offer_master",
            {"id": f"eq.{offer['id']}"},
            {"membership_name": None},
        )
        cleared_names += 1

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "active_offers": len(offers),
        "plans": len(plans),
        "ended_stale_offers": len(ended_ids),
        "backfilled_fk": backfilled,
        "cleared_legacy_membership_name": cleared_names,
        "unmatched": len(unmatched),
    }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report = out_dir / f"migrate_offer_membership_fk_{stamp}.json"
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if unmatched:
        csv_path = out_dir / f"migrate_offer_membership_fk_unmatched_{stamp}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["id", "source_url", "service_name", "membership_name"])
            writer.writeheader()
            for row in unmatched:
                writer.writerow(
                    {
                        "id": row.get("id"),
                        "source_url": row.get("source_url"),
                        "service_name": row.get("service_name"),
                        "membership_name": row.get("membership_name"),
                    }
                )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
