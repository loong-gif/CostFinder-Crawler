#!/usr/bin/env python3
"""Backfill stable business-field fingerprints for existing offers."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.offer_fingerprint import compute_offer_fingerprint  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402


def main() -> int:
    client = load_supabase_client(ROOT)
    offers = fetch_all_rows(
        client,
        "promo_offer_master",
        "id,business_id,membership_plan_id,promotion_id,regular_price,discount_price,discount_percent,discount_amount,is_active,offer_raw_text,offer_fingerprint,promo_offer_items(service_id)",
        order="id.asc",
    )
    promotions = {
        int(row["promotion_id"]): row
        for row in fetch_all_rows(client, "clinic_promotions", "promotion_id,source_url", order="promotion_id.asc")
        if row.get("promotion_id") is not None
    }
    memberships = {
        int(row["plan_id"]): row
        for row in fetch_all_rows(client, "clinic_memberships", "plan_id,source_url", order="plan_id.asc")
        if row.get("plan_id") is not None
    }
    services = {
        int(row["service_id"]): row
        for row in fetch_all_rows(client, "clinic_services", "service_id,service_name,unit_type", order="service_id.asc")
        if row.get("service_id") is not None
    }
    changed = 0
    skipped_invalid = []
    for offer in offers:
        if offer.get("is_active") and max(float(offer.get(field) or 0) for field in ("discount_price", "discount_percent", "discount_amount")) <= 0:
            skipped_invalid.append(int(offer["id"]))
            continue
        if len(str(offer.get("offer_fingerprint") or "")) == 40:
            continue
        items = offer.get("promo_offer_items") or []
        service = next((services.get(int(item["service_id"])) for item in items if item.get("service_id") in services), None)
        source = promotions.get(int(offer["promotion_id"]), {}) if offer.get("promotion_id") is not None else memberships.get(int(offer["membership_plan_id"]), {}) if offer.get("membership_plan_id") is not None else {}
        fingerprint = compute_offer_fingerprint(
            source_url=source.get("source_url", ""),
            service_name=(service or {}).get("service_name") or str(offer.get("offer_raw_text") or "Offer"),
            unit_type=(service or {}).get("unit_type", ""),
            regular_price=offer.get("regular_price"),
            discount_price=offer.get("discount_price"),
            offer_raw_text=offer.get("offer_raw_text", ""),
            business_id=offer.get("business_id"),
            promotion_id=offer.get("promotion_id"),
            membership_plan_id=offer.get("membership_plan_id"),
            discount_percent=offer.get("discount_percent"),
            discount_amount=offer.get("discount_amount"),
        )
        if fingerprint != offer.get("offer_fingerprint"):
            try:
                client.update_row("promo_offer_master", {"id": f"eq.{offer['id']}"}, {"offer_fingerprint": fingerprint})
            except Exception as exc:
                response = getattr(exc, "response", None)
                detail = getattr(response, "text", "")
                raise RuntimeError(f"fingerprint update failed for offer {offer['id']}: {detail}") from exc
            changed += 1
    print({"offers": len(offers), "fingerprints_updated": changed, "skipped_active_without_discount": len(skipped_invalid), "skipped_ids": skipped_invalid})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
