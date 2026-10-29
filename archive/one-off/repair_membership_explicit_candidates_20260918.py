#!/usr/bin/env python3
"""Repair only the rows created by the interrupted explicit-candidate apply."""
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

from apply_membership_explicit_candidates_20260918 import build_plan  # noqa: E402
from apply_offer_plan_20260918 import fetch_all, load_client  # noqa: E402
from utils.offer_plan_20260918 import split_offer_fingerprint  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def repair(client, plan: dict) -> dict:
    offers = fetch_all(
        client,
        "promo_offer_master",
        "id,business_id,membership_plan_id,is_active,offer_fingerprint,offer_raw_text,promo_offer_items(offer_item_id,service_id,unit_price,delivered_units)",
        order="id.asc",
    )
    repaired = []
    skipped = []
    for spec in plan["candidates"]:
        expected_fp = split_offer_fingerprint(
            source_key=f"m:{spec['plan_id']}",
            service_name=spec["service_name"],
            offer_raw_text=spec["raw"],
            regular_price=None,
            discount_price=spec["discount_price"],
            discount_percent=spec["discount_percent"],
            discount_amount=spec["discount_amount"],
            price_model=spec["price_model"],
        )
        row = next((offer for offer in offers if offer.get("offer_fingerprint") == expected_fp), None)
        if not row and spec.get("existing_matching_offer_ids"):
            row = next(
                (offer for offer in offers if int(offer["id"]) in spec["existing_matching_offer_ids"]),
                None,
            )
        if not row:
            row = next(
                (
                    offer for offer in offers
                    if offer.get("membership_plan_id") == spec["plan_id"]
                    and offer.get("offer_raw_text") is None
                    and any(item.get("service_id") == spec["service_id"] for item in (offer.get("promo_offer_items") or []))
                ),
                None,
            )
        if not row:
            skipped.append({"candidate": spec, "reason": "fingerprint_row_not_found"})
            continue
        master = {
            "offer_raw_text": spec["raw"],
            "is_membership_required": True,
            "is_new_customer_required": False,
            "conditions": spec["conditions"],
            "discount_percent": 100.0 if spec["discount_price"] == 0 else spec["discount_percent"],
            "is_active": True,
            "updated_at": now(),
        }
        client.update_row("promo_offer_master", {"id": f"eq.{row['id']}"}, master)
        item_updates = []
        for item in row.get("promo_offer_items") or []:
            payload = {
                "delivered_units": None,
                "unit_price": spec["unit_price"] if spec["price_model"] == "per_unit" else None,
                "updated_at": now(),
            }
            client.update_row("promo_offer_items", {"offer_item_id": f"eq.{item['offer_item_id']}"}, payload)
            item_updates.append({"offer_item_id": item["offer_item_id"], **payload})
        repaired.append({"offer_id": row["id"], "item_updates": item_updates})
    return {"repaired": repaired, "skipped": skipped, "errors": []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "offer_plan_20260918" / "membership_explicit_candidates_20260918_repair.json")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    client = load_client()
    plan = build_plan(client)
    result = {**plan, "mode": "dry_run"}
    if args.apply:
        result = {**plan, "mode": "apply", "repair_result": repair(client, plan)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "mode": result["mode"], "repairable": len(plan["candidates"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
