"""Pure acceptance gates for the CostFinder P0/P1 repair run."""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def acceptance_gate_counts(
    offers: Sequence[Mapping[str, Any]],
    promotions: Mapping[Any, Mapping[str, Any]],
    services: Mapping[Any, Mapping[str, Any]],
) -> dict[str, int]:
    """Count hard data gates without changing or re-reading the data."""
    active = [row for row in offers if bool(row.get("is_active"))]
    active_ids = {row.get("id") for row in active}
    counts = {
        "active_offer_on_inactive_promotion": 0,
        "active_item_service_id_null": 0,
        "per_unit_mapped_item_missing_unit_price": 0,
        "item_name_mismatch_service_name": 0,
    }
    for offer in active:
        promotion = promotions.get(offer.get("promotion_id"))
        if offer.get("promotion_id") is not None and promotion and not bool(promotion.get("is_active")):
            counts["active_offer_on_inactive_promotion"] += 1
        for item in offer.get("promo_offer_items") or []:
            service_id = item.get("service_id")
            if service_id is None:
                counts["active_item_service_id_null"] += 1
                continue
            service = services.get(service_id)
            if (
                str(offer.get("price_model") or "").lower() == "per_unit"
                and offer.get("discount_price") is not None
                and item.get("unit_price") is None
            ):
                counts["per_unit_mapped_item_missing_unit_price"] += 1
            if service and str(item.get("item_name") or "").strip().casefold() != str(service.get("service_name") or "").strip().casefold():
                counts["item_name_mismatch_service_name"] += 1
    return counts


def acceptance_passes(counts: Mapping[str, int]) -> bool:
    """Return true only when every hard data gate is zero."""
    return all(value == 0 for value in counts.values())
