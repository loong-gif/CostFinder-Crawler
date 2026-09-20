#!/usr/bin/env python3
"""Apply one source-backed membership package candidate with a dry-run default."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_offer_plan_20260918 import fetch_all, load_client  # noqa: E402
from utils.offer_plan_20260918 import split_offer_fingerprint  # noqa: E402

PLAN_ID = 327
BUSINESS_ID = 246
RAW_TEXT = "Naked Glow — Sculptra + PRF: $599 per treatment"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state(client):
    offers = fetch_all(
        client,
        "promo_offer_master",
        "id,business_id,membership_plan_id,is_active,is_package,price_model,regular_price,discount_price,discount_percent,discount_amount,is_membership_required,is_new_customer_required,offer_fingerprint,offer_raw_text,conditions,updated_at,promo_offer_items(offer_item_id,item_name,service_id,delivered_units,unit_price,updated_at)",
        order="id.asc",
    )
    services = fetch_all(
        client,
        "clinic_services",
        "service_id,business_id,service_name,regular_price,unit_type,source_url",
        order="service_id.asc",
    )
    memberships = fetch_all(
        client,
        "clinic_memberships",
        "plan_id,business_id,membership_name,source_url,is_offer_extracted,updated_at",
        order="plan_id.asc",
    )
    return offers, services, memberships


def build_plan(client) -> dict:
    offers, services, memberships = state(client)
    membership = next((row for row in memberships if int(row["plan_id"]) == PLAN_ID), None)
    if not membership:
        raise RuntimeError(f"membership plan {PLAN_ID} not found")
    if int(membership.get("business_id")) != BUSINESS_ID:
        raise RuntimeError("membership business changed")
    if "nakedmd.com" not in str(membership.get("source_url") or "").casefold():
        raise RuntimeError("membership source URL is not the verified NakedMD source")

    required = {
        name: next(
            (row for row in services if row.get("business_id") == BUSINESS_ID and row.get("service_name") == name),
            None,
        )
        for name in ("Sculptra", "PRF")
    }
    missing = [name for name, row in required.items() if not row]
    if missing:
        raise RuntimeError(f"missing same-business services: {missing}")

    existing = [
        offer for offer in offers
        if offer.get("membership_plan_id") == PLAN_ID and offer.get("is_active")
        and {item.get("item_name") for item in (offer.get("promo_offer_items") or [])} == {"Sculptra", "PRF"}
    ]
    spec = {
        "business_id": BUSINESS_ID,
        "membership_plan_id": PLAN_ID,
        "is_package": True,
        "price_model": "total",
        "regular_price": None,
        "discount_price": 599.0,
        "discount_percent": None,
        "discount_amount": None,
        "is_membership_required": True,
        "is_new_customer_required": False,
        "offer_raw_text": RAW_TEXT,
        "conditions": ["Applicable membership tier: NakedMD Members Club"],
        "items": [
            {"service_name": name, "service_id": required[name]["service_id"], "delivered_units": None}
            for name in ("Sculptra", "PRF")
        ],
        "source_evidence": {"source_url": membership["source_url"], "evidence": RAW_TEXT},
        "existing_matching_offer_ids": [int(row["id"]) for row in existing],
        "action": "REVIEW" if existing else "CREATE",
    }
    return {"generated_at": now(), "mode": "dry_run", "plan": spec}


def apply(client, plan: dict) -> dict:
    spec = plan["plan"]
    if spec["existing_matching_offer_ids"]:
        return {"mode": "apply", "created": [], "skipped_existing": spec["existing_matching_offer_ids"], "errors": []}
    master = {
        key: spec[key]
        for key in (
            "business_id", "membership_plan_id", "is_package", "price_model", "regular_price",
            "discount_price", "discount_percent", "discount_amount", "is_membership_required",
            "is_new_customer_required", "offer_raw_text", "conditions",
        )
    }
    master["promotion_id"] = None
    master["is_active"] = False
    master["offer_fingerprint"] = split_offer_fingerprint(
        source_key=f"m:{PLAN_ID}",
        service_name="Sculptra + PRF",
        offer_raw_text=spec["offer_raw_text"],
        regular_price=spec["regular_price"],
        discount_price=spec["discount_price"],
        discount_percent=spec["discount_percent"],
        discount_amount=spec["discount_amount"],
        price_model=spec["price_model"],
    )
    inserted = client.insert_rows("promo_offer_master", [master])[0]
    offer_id = int(inserted["id"])
    items = [
        {
            "offer_id": offer_id,
            "item_name": item["service_name"],
            "service_id": item["service_id"],
            "delivered_units": None,
        }
        for item in spec["items"]
    ]
    inserted_items = client.insert_rows("promo_offer_items", items)
    for item in inserted_items:
        client.update_row(
            "promo_offer_items",
            {"offer_item_id": f"eq.{item['offer_item_id']}"},
            {"delivered_units": None, "updated_at": now()},
        )
    activated = client.update_row(
        "promo_offer_master",
        {"id": f"eq.{offer_id}"},
        {"is_active": True, "updated_at": now()},
    )
    return {"mode": "apply", "created": [{"offer_id": offer_id, "items": inserted_items, "activated": bool(activated)}], "errors": []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "offer_plan_20260918" / "membership_package_candidate_20260918.json")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    client = load_client()
    plan = build_plan(client)
    result = plan
    if args.apply:
        result = {**plan, "mode": "apply", "apply_result": apply(client, plan)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "mode": result["mode"], "action": plan["plan"]["action"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
