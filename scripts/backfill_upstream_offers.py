#!/usr/bin/env python3
"""Create source-proven upstream offers missing from promotion/membership extraction."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.offer_fingerprint import compute_offer_fingerprint  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402


SOURCE_OFFERS = [
    {"source_type": "promotion", "source_id": 3549, "business_id": 3049, "raw": "Botox: $13/unit when getting 50+ units.", "service": "Botox", "regular": 14, "discount": 13, "amount": 1, "percent": 7.14, "model": "per_unit", "quantity": 50},
    {"source_type": "promotion", "source_id": 3549, "business_id": 3049, "raw": "100 units of Botox: $1,050, a savings of $350.", "service": "Botox", "regular": 1400, "discount": 1050, "amount": 350, "percent": 25, "model": "total", "quantity": 100},
    {"source_type": "promotion", "source_id": 3551, "business_id": 3097, "raw": "Botox: 30% off at $9.45/unit; 20 units $189 vs $270 regular.", "service": "Botox", "regular": 13.5, "discount": 9.45, "amount": 4.05, "percent": 30, "model": "per_unit", "quantity": 20},
    {"source_type": "promotion", "source_id": 3551, "business_id": 3097, "raw": "Sculptra Bio Stimulator x6: $2,990 vs $4,800.", "service": "Sculptra", "regular": 4800, "discount": 2990, "amount": 1810, "percent": 37.71, "model": "total", "quantity": 6},
    {"source_type": "promotion", "source_id": 3551, "business_id": 3097, "raw": "Restylane Kysse lip filler: 20% off, $600 vs $750.", "service": "Restylane Kysse", "regular": 750, "discount": 600, "amount": 150, "percent": 20, "model": "total", "quantity": None},
    {"source_type": "promotion", "source_id": 3543, "business_id": 359, "raw": "New patients only: Neurotoxin treatments for $8/unit with 50+ units.", "service": "Botox", "regular": 12, "discount": 8, "amount": 4, "percent": 33.33, "model": "per_unit", "quantity": 50},
    {"source_type": "membership", "source_id": 252, "business_id": 1181, "raw": "The Refresh: 4 Full Correction Neurotoxin Treatments/Year, up to 60 units Jeuveau or Letybo.", "service": "Jeuveau", "regular": None, "discount": 0, "amount": None, "percent": 100, "model": "total", "quantity": 60, "alternate_service": "Letybo"},
    {"source_type": "membership", "source_id": 347, "business_id": 1421, "raw": "Bella VIP Premier: $75 OFF each syringe of Filler (unlimited use).", "service": "Dermal Filler", "regular": None, "discount": None, "amount": 75, "percent": None, "model": "per_unit", "quantity": None},
]


def main() -> int:
    client = load_supabase_client(ROOT)
    promotions = {int(r["promotion_id"]): r for r in fetch_all_rows(client, "clinic_promotions", "promotion_id,business_id,source_url", order="promotion_id.asc")}
    memberships = {int(r["plan_id"]): r for r in fetch_all_rows(client, "clinic_memberships", "plan_id,business_id,source_url", order="plan_id.asc")}
    services = fetch_all_rows(client, "clinic_services", "service_id,business_id,service_name,unit_type", order="service_id.asc")
    service_by_key = {(int(r["business_id"]), str(r["service_name"]).strip().casefold()): r for r in services}
    existing = fetch_all_rows(client, "promo_offer_master", "id,business_id,promotion_id,membership_plan_id,offer_fingerprint", order="id.asc")
    existing_fingerprints = {str(r.get("offer_fingerprint") or "") for r in existing}
    created_offers = 0
    created_items = 0
    created_services = 0

    for spec in SOURCE_OFFERS:
        source = promotions.get(spec["source_id"]) if spec["source_type"] == "promotion" else memberships.get(spec["source_id"])
        if not source or int(source["business_id"]) != spec["business_id"]:
            raise RuntimeError(f"source ownership changed for {spec['source_type']} {spec['source_id']}")
        service_key = (spec["business_id"], spec["service"].casefold())
        service = service_by_key.get(service_key)
        if service is None:
            service = client.insert_rows("clinic_services", [{"business_id": spec["business_id"], "service_name": spec["service"], "unit_type": "unit" if spec["model"] == "per_unit" else None, "source_url": source.get("source_url")}])[0]
            service_by_key[service_key] = service
            created_services += 1
        membership_plan_id = spec["source_id"] if spec["source_type"] == "membership" else None
        promotion_id = spec["source_id"] if spec["source_type"] == "promotion" else None
        fp = compute_offer_fingerprint(source_url=source.get("source_url", ""), service_name=spec["service"], unit_type=service.get("unit_type", ""), regular_price=spec["regular"], discount_price=spec["discount"], offer_raw_text=spec["raw"], business_id=spec["business_id"], promotion_id=promotion_id, membership_plan_id=membership_plan_id, discount_percent=spec["percent"], discount_amount=spec["amount"])
        if fp in existing_fingerprints:
            continue
        master = {"business_id": spec["business_id"], "promotion_id": promotion_id, "membership_plan_id": membership_plan_id, "regular_price": spec["regular"], "discount_price": spec["discount"], "discount_amount": spec["amount"], "discount_percent": spec["percent"], "is_membership_required": membership_plan_id is not None, "offer_fingerprint": fp, "offer_raw_text": spec["raw"], "is_active": True, "is_new_customer_required": False, "price_model": spec["model"]}
        offer_id = int(client.insert_rows("promo_offer_master", [master])[0]["id"])
        item_specs = [(service, spec["quantity"])]
        if spec.get("alternate_service"):
            alt_key = (spec["business_id"], spec["alternate_service"].casefold())
            alt = service_by_key.get(alt_key)
            if alt is None:
                alt = client.insert_rows("clinic_services", [{"business_id": spec["business_id"], "service_name": spec["alternate_service"], "unit_type": "unit", "source_url": source.get("source_url")}])[0]
                service_by_key[alt_key] = alt
                created_services += 1
            item_specs.append((alt, spec["quantity"]))
        client.insert_rows("promo_offer_items", [{"offer_id": offer_id, "service_id": int(item[0]["service_id"]), "quantity": item[1]} for item in item_specs])
        existing_fingerprints.add(fp)
        created_offers += 1
        created_items += len(item_specs)

    for spec in SOURCE_OFFERS:
        table = "clinic_promotions" if spec["source_type"] == "promotion" else "clinic_memberships"
        key = "promotion_id" if spec["source_type"] == "promotion" else "plan_id"
        client.update_row(table, {key: f"eq.{spec['source_id']}"}, {"is_offer_extracted": True})
    print({"created_offers": created_offers, "created_items": created_items, "created_services": created_services})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
