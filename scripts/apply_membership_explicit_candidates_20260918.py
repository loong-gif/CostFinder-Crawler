#!/usr/bin/env python3
"""Apply explicit membership candidates grounded in captured page lines."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_offer_plan_20260918 import fetch_all, load_client  # noqa: E402
from utils.offer_plan_20260918 import split_offer_fingerprint  # noqa: E402


CANDIDATES = (
    {
        "plan_id": 479,
        "service_name": "Botox",
        "raw": "$11/ unit Botox + Jeuveau",
        "price_model": "per_unit",
        "discount_price": 11.0,
        "unit_price": 11.0,
        "discount_percent": None,
        "discount_amount": None,
        "evidence_match": "$11/ unit Botox + Jeuveau",
    },
    {
        "plan_id": 479,
        "service_name": "Jeuveau",
        "raw": "$11/ unit Botox + Jeuveau",
        "price_model": "per_unit",
        "discount_price": 11.0,
        "unit_price": 11.0,
        "discount_percent": None,
        "discount_amount": None,
        "evidence_match": "$11/ unit Botox + Jeuveau",
    },
    {
        "plan_id": 515,
        "service_name": "Dermal Filler",
        "raw": "Dermal filler (est.): $105/syringe",
        "price_model": "per_unit",
        "discount_price": 105.0,
        "unit_price": 105.0,
        "discount_percent": None,
        "discount_amount": None,
        "evidence_match": "Dermal filler (est.) $70/syringe $105/syringe",
    },
    {
        "plan_id": 515,
        "service_name": "SkinPen Microneedling",
        "raw": "SkinPen Microneedling: $61",
        "price_model": "total",
        "discount_price": 61.0,
        "unit_price": None,
        "discount_percent": None,
        "discount_amount": None,
        "evidence_match": "SkinPen Microneedling $41 $61",
    },
    {
        "plan_id": 289,
        "service_name": "Laser Hair Removal",
        "raw": "Laser Hair Removal ($100 value): Choice of underarms, chin, or lip.",
        "price_model": "total",
        "discount_price": 0.0,
        "unit_price": None,
        "discount_percent": 100.0,
        "discount_amount": None,
        "conditions_extra": ["Quarterly choice: underarms, chin, or lip"],
        "evidence_match": "Laser Hair Removal ($100 value): Choice of underarms, chin, or lip.",
    },
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_pages() -> dict[str, str]:
    path = ROOT / ".firecrawl" / "clinic_memberships_plan_20260918_resume_scrapes" / "firecrawl_main_scrapes_resumed.json"
    pages = json.loads(path.read_text(encoding="utf-8"))
    return {str(row.get("requested_url")): str(row.get("markdown") or "") for row in pages}


def normalized_evidence(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def build_plan(client) -> dict:
    offers = fetch_all(
        client,
        "promo_offer_master",
        "id,business_id,membership_plan_id,is_active,offer_raw_text,promo_offer_items(item_name,service_id)",
        order="id.asc",
    )
    services = fetch_all(client, "clinic_services", "service_id,business_id,service_name", order="service_id.asc")
    memberships = fetch_all(
        client,
        "clinic_memberships",
        "plan_id,business_id,membership_name,source_url,minimum_commitment_months,is_offer_extracted",
        order="plan_id.asc",
    )
    pages = read_pages()
    by_plan = {int(row["plan_id"]): row for row in memberships}
    specs = []
    review = []
    for candidate in CANDIDATES:
        plan = by_plan.get(candidate["plan_id"])
        if not plan:
            review.append({"candidate": candidate, "reason": "membership_missing"})
            continue
        page_text = pages.get(str(plan.get("source_url")), "")
        evidence = normalized_evidence(candidate.get("evidence_match") or candidate["raw"])
        if not evidence or evidence not in normalized_evidence(page_text):
            review.append({"candidate": candidate, "reason": "evidence_line_not_found_in_captured_page"})
            continue
        service = next(
            (
                row for row in services
                if row.get("business_id") == plan.get("business_id")
                and row.get("service_name") == candidate["service_name"]
            ),
            None,
        )
        if not service:
            review.append({"candidate": candidate, "reason": "same_business_service_missing"})
            continue
        duplicate = [
            offer for offer in offers
            if offer.get("membership_plan_id") == candidate["plan_id"]
            and offer.get("is_active")
            and any(item.get("service_id") == service["service_id"] for item in (offer.get("promo_offer_items") or []))
        ]
        specs.append(
            {
                **candidate,
                "business_id": plan["business_id"],
                "membership_name": plan["membership_name"],
                "membership_source_url": plan["source_url"],
                "service_id": service["service_id"],
                "conditions": [f"Applicable membership tier: {plan['membership_name']}"]
                + ([f"Minimum commitment: {plan['minimum_commitment_months']} months"] if plan.get("minimum_commitment_months") else [])
                + list(candidate.get("conditions_extra") or []),
                "existing_matching_offer_ids": [int(offer["id"]) for offer in duplicate],
                "action": "REVIEW" if duplicate else "CREATE",
                "source_evidence": candidate.get("evidence_match") or candidate["raw"],
            }
        )
    return {"generated_at": now(), "mode": "dry_run", "candidates": specs, "review": review}


def apply(client, plan: dict) -> dict:
    created = []
    skipped = []
    for spec in plan["candidates"]:
        if spec["existing_matching_offer_ids"]:
            skipped.append({"service_name": spec["service_name"], "offer_ids": spec["existing_matching_offer_ids"]})
            continue
        master = {
            "business_id": spec["business_id"],
            "price_model": spec["price_model"],
            "discount_price": spec["discount_price"],
            "discount_percent": spec["discount_percent"],
            "discount_amount": spec["discount_amount"],
            "is_membership_required": True,
            "is_new_customer_required": False,
            "offer_raw_text": spec["raw"],
            "conditions": spec["conditions"],
        }
        master.update(
            {
                "promotion_id": None,
                "membership_plan_id": spec["plan_id"],
                "regular_price": None,
                "is_package": False,
                "is_active": False,
                "offer_fingerprint": split_offer_fingerprint(
                    source_key=f"m:{spec['plan_id']}",
                    service_name=spec["service_name"],
                    offer_raw_text=spec["raw"],
                    regular_price=None,
                    discount_price=spec["discount_price"],
                    discount_percent=spec["discount_percent"],
                    discount_amount=spec["discount_amount"],
                    price_model=spec["price_model"],
                ),
            }
        )
        row = client.insert_rows("promo_offer_master", [master])[0]
        offer_id = int(row["id"])
        item = client.insert_rows(
            "promo_offer_items",
            [{
                "offer_id": offer_id,
                "item_name": spec["service_name"],
                "service_id": spec["service_id"],
                "delivered_units": None,
                "unit_price": spec["unit_price"] if spec["price_model"] == "per_unit" else None,
            }],
        )[0]
        client.update_row(
            "promo_offer_items",
            {"offer_item_id": f"eq.{item['offer_item_id']}"},
            {
                "delivered_units": None,
                "unit_price": spec["unit_price"] if spec["price_model"] == "per_unit" else None,
                "updated_at": now(),
            },
        )
        activated = client.update_row(
            "promo_offer_master",
            {"id": f"eq.{offer_id}"},
            {"is_active": True, "updated_at": now()},
        )
        created.append({"offer_id": offer_id, "service_id": spec["service_id"], "activated": bool(activated)})
    return {"created": created, "skipped": skipped, "errors": []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "offer_plan_20260918" / "membership_explicit_candidates_20260918.json")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    client = load_client()
    plan = build_plan(client)
    result = plan
    if args.apply:
        result = {**plan, "mode": "apply", "apply_result": apply(client, plan)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "mode": result["mode"], "create_candidates": sum(item["action"] == "CREATE" for item in plan["candidates"]), "review": len(plan["review"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
