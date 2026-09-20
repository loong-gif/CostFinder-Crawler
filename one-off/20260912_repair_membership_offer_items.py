"""Repair active membership Offers without items.

Reads promo_offer_master, clinic_memberships and clinic_services. Defaults to
dry-run; pass --apply to write the planned item rows and status changes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.membership_offer_items import expand_membership_offer_services
from utils.schema_contract import OFFER_MASTER_WITH_ITEMS_SELECT
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key
from utils.promo_offer_items_db import upsert_offer_items

def client() -> SupabaseRestClient:
    load_dotenv(ROOT / ".env")
    return SupabaseRestClient(os.environ["SUPABASE_URL"], get_supabase_writer_key())


def fetch_targets(db: SupabaseRestClient) -> tuple[list[dict], dict[int, list[dict]]]:
    offers = db.fetch_rows(
        "promo_offer_master", OFFER_MASTER_WITH_ITEMS_SELECT,
        filters={"is_active": "eq.true", "membership_plan_id": "not.is.null", "promotion_id": "is.null"},
        limit=5000, order="id.asc",
    )
    offers = [row for row in offers if not (row.get("promo_offer_items") or [])]
    businesses = sorted({int(row["business_id"]) for row in offers if row.get("business_id") is not None})
    services: dict[int, list[dict]] = defaultdict(list)
    for start in range(0, len(businesses), 40):
        chunk = businesses[start : start + 40]
        rows = db.fetch_rows("clinic_services", "service_id,business_id,service_name,service_category,unit_type,regular_price", filters={"business_id": f"in.({','.join(map(str, chunk))})"}, limit=5000)
        for row in rows:
            services[int(row["business_id"])].append(row)
    return offers, services


def build_plan(offers: list[dict], services: dict[int, list[dict]]) -> list[dict]:
    plan = []
    for offer in offers:
        scope, matched, reason = expand_membership_offer_services(offer.get("offer_raw_text") or "", services.get(int(offer["business_id"]), []))
        plan.append({
            "offer_id": int(offer["id"]),
            "business_id": int(offer["business_id"]),
            "membership_plan_id": int(offer["membership_plan_id"]),
            "offer_raw_text": offer.get("offer_raw_text"),
            "scope": scope,
            "service_ids": [int(row["service_id"]) for row in matched],
            "service_names": [str(row["service_name"]) for row in matched],
            "reason": reason,
            "action": "REVIEW" if reason else "REPAIR",
        })
    return plan


def apply_plan(db: SupabaseRestClient, plan: list[dict]) -> dict:
    result = {"repaired": 0, "review": 0, "conflicts": []}
    for item in plan:
        current = db.fetch_rows("promo_offer_master", "id,offer_raw_text,is_active,promo_offer_items(offer_item_id)", filters={"id": f"eq.{item['offer_id']}"}, limit=1)
        if not current or current[0].get("offer_raw_text") != item["offer_raw_text"] or current[0].get("promo_offer_items"):
            result["conflicts"].append(item["offer_id"])
            continue
        if item["reason"]:
            db.update_row("promo_offer_master", {"id": f"eq.{item['offer_id']}"}, {"is_active": False, "updated_at": datetime.now(timezone.utc).isoformat()})
            result["review"] += 1
            continue
        db.update_row("promo_offer_master", {"id": f"eq.{item['offer_id']}"}, {"is_active": False, "updated_at": datetime.now(timezone.utc).isoformat()})
        upsert_offer_items(db, item["offer_id"], [{"service_id": sid} for sid in item["service_ids"]])
        db.update_row("promo_offer_master", {"id": f"eq.{item['offer_id']}"}, {"is_active": True, "updated_at": datetime.now(timezone.utc).isoformat()})
        result["repaired"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", default="output/results/membership_offer_items_repair_20260912.json")
    args = parser.parse_args()
    db = client()
    offers, services = fetch_targets(db)
    plan = build_plan(offers, services)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": "apply" if args.apply else "dry_run", "target_count": len(plan), "plan": plan}
    if args.apply:
        payload["result"] = apply_plan(db, plan)
    path = ROOT / args.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("mode", "target_count", "result") if k in payload}, ensure_ascii=False))


if __name__ == "__main__":
    main()
