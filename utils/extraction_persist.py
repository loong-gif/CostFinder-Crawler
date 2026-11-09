"""Centralized extraction routing + persistence for clinic tables."""
from __future__ import annotations

from typing import Any, Optional

from utils.clinic_service_extraction import (
    offer_to_clinic_service_item,
    pick_best_service_items,
)
from utils.extraction_persist_offer import persist_promo_offer
from utils.extraction_persist_pricing import route_offer
from utils.extraction_persist_records import persist_service_item
from utils.offer_purchase_semantics import normalize_purchase_offers


def _coalesce_source_offer_items(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one shared-price source line together when extraction splits items."""
    grouped: list[dict[str, Any]] = []
    for offer in offers:
        terms = {key: value for key, value in offer.items() if key != "items"}
        match = next((row for row in grouped if (offer.get("is_package") is True or offer.get("purchase_type") == "bundle") and
            {key: value for key, value in row.items() if key != "items"} == terms), None)
        if match is None:
            grouped.append({**offer, "items": [dict(item) for item in offer.get("items") or []]})
            continue
        match["items"] = match["items"] + [
            dict(item) for item in offer.get("items") or [] if item not in match["items"]
        ]
    return grouped


def route_and_persist_extraction(
    client: Any,
    *,
    business_id: int,
    promotion_id: int,
    source_url: str,
    offers: list[dict[str, Any]],
    evidence: str,
    membership_plan_id: Optional[int] = None,
    seen_fingerprints: set[str] | None = None,
) -> dict[str, Any]:
    """Route list-price rows to clinic_services and discount rows to promo tables."""
    stats = {
        "services": 0,
        "promos": 0,
        "quarantined": 0,
        "skipped": 0,
        "service_writes": [],
        "promo_writes": [],
    }
    service_candidates: list[dict[str, Any]] = []
    normalized, purchase_reviews = normalize_purchase_offers(offers, evidence)
    stats["purchase_reviews"] = purchase_reviews
    stats["quarantined"] += sum(review["reason"] != "generic_offer_scope" for review in purchase_reviews)
    for offer in _coalesce_source_offer_items(normalized):
        kind = route_offer(offer)
        if kind == "service":
            item = offer_to_clinic_service_item(offer)
            if item:
                service_candidates.append(item)
            else:
                stats["skipped"] += 1
            continue
        if kind == "quarantine":
            stats["quarantined"] += 1
            continue
        write = persist_promo_offer(
            client,
            business_id=business_id,
            promotion_id=promotion_id,
            source_url=source_url,
            offer=offer,
            evidence=evidence,
            membership_plan_id=membership_plan_id,
            seen_fingerprints=seen_fingerprints,
        )
        stats["promo_writes"].append(write)
        if write.get("accepted"):
            stats["promos"] += 1
        elif write.get("action") == "quarantined":
            stats["quarantined"] += 1
        else:
            stats["skipped"] += 1

    for item in pick_best_service_items(service_candidates, evidence, source_url=source_url):
        write = persist_service_item(
            client,
            business_id=business_id,
            item=item,
            source_url=source_url,
            evidence=evidence,
        )
        stats["service_writes"].append(write)
        if write.get("accepted") and write.get("service_id"):
            stats["services"] += 1
    return stats
