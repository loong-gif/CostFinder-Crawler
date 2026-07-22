"""Centralized extraction routing + persistence for clinic tables."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from utils.clinic_promotions_db import upsert_promotion
from utils.clinic_service_extraction import (
    attach_service_ids_to_items,
    is_promo_offer,
    offer_to_clinic_service_item,
    pick_best_service_items,
    upsert_extracted_service,
)
from utils.offer_extraction_llm import canonicalize_service_name
from utils.offer_fingerprint import compute_offer_fingerprint
from utils.offer_scope_filter import exclude_reason, should_exclude_from_offer_master
from utils.promo_offer_items_db import upsert_offer_items
from utils.recent_raw_extraction import (
    build_promotion_content,
    expand_promotion_content,
    validate_membership,
    validate_promotion,
    validate_service,
)
from utils.schema_contract import TABLE_CLINIC_MEMBERSHIPS, TABLE_CLINIC_PROMOTIONS, TABLE_PROMO_OFFER_MASTER

RouteKind = Literal["service", "promo", "quarantine"]


def _positive(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def route_offer(offer: dict[str, Any]) -> RouteKind:
    if should_exclude_from_offer_master(offer):
        return "quarantine"
    if is_promo_offer(offer):
        return "promo"
    if offer_to_clinic_service_item(offer) is not None:
        return "service"
    return "quarantine"


def infer_price_model(offer: dict[str, Any]) -> str:
    raw = str(offer.get("offer_raw_text") or "").lower()
    if "/unit" in raw or "per unit" in raw:
        return "per_unit"
    if "starting at" in raw or "from " in raw:
        return "from"
    return "total"


def build_master_from_offer(
    offer: dict[str, Any],
    *,
    business_id: int,
    promotion_id: int,
    source_url: str,
    membership_plan_id: Optional[int] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not is_promo_offer(offer):
        raise ValueError(
            "promo_offer_master requires discount_price, discount_percent, or discount_amount"
        )
    items_in = offer.get("items") or []
    first = items_in[0] if items_in else {}
    service_name = canonicalize_service_name(
        first.get("service_name"),
        offer.get("offer_raw_text"),
    ) or "Others"
    master: dict[str, Any] = {
        "business_id": business_id,
        "promotion_id": promotion_id,
        "is_active": True,
        "is_new_customer_required": bool(offer.get("is_new_customer_required")),
        "is_membership_required": bool(offer.get("is_membership_required")),
        "offer_raw_text": str(offer.get("offer_raw_text") or service_name)[:4000],
        "price_model": infer_price_model(offer),
    }
    if offer.get("regular_price") is not None:
        master["regular_price"] = float(offer["regular_price"])
    if offer.get("discount_price") is not None:
        master["discount_price"] = float(offer["discount_price"])
    if offer.get("discount_percent") is not None:
        master["discount_percent"] = float(offer["discount_percent"])
    if offer.get("discount_amount") is not None:
        master["discount_amount"] = float(offer["discount_amount"])
    if membership_plan_id is not None and offer.get("is_membership_required"):
        master["membership_plan_id"] = membership_plan_id
    unit_type = str(first.get("unit_type") or "")
    raw_text = str(offer.get("offer_raw_text") or "")
    master["offer_fingerprint"] = compute_offer_fingerprint(
        source_url=source_url,
        service_name=service_name,
        unit_type=unit_type,
        regular_price=master.get("regular_price"),
        discount_price=master.get("discount_price"),
        offer_raw_text=raw_text,
    )
    item_rows: list[dict[str, Any]] = []
    for raw in items_in:
        name = canonicalize_service_name(
            raw.get("service_name"),
            raw.get("service_area"),
            raw_text,
        ) or str(raw.get("service_name") or "Offer item")
        item: dict[str, Any] = {"service_name": name}
        if raw.get("quantity") is not None:
            item["quantity"] = raw["quantity"]
        if raw.get("unit_price") is not None:
            item["unit_price"] = raw["unit_price"]
        item_rows.append(item)
    if not item_rows:
        item_rows = [{"service_name": service_name}]
    return master, item_rows


def persist_service_item(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
) -> dict[str, Any]:
    decision = validate_service(item, evidence)
    if not decision.accepted:
        return {"accepted": False, "reason": decision.reason, "action": "skipped"}
    return upsert_extracted_service(
        client,
        business_id=business_id,
        item=item,
        source_url=source_url,
        evidence=evidence,
    )


def persist_membership_item(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
) -> dict[str, Any]:
    decision = validate_membership(item, evidence)
    if not decision.accepted:
        return {"accepted": False, "reason": decision.reason, "action": "skipped"}
    row = {
        "business_id": business_id,
        "membership_name": str(item.get("membership_name") or "").strip(),
        "membership_price": float(item["membership_price"]),
        "billing_period": item.get("billing_period"),
        "minimum_commitment_months": item.get("minimum_commitment_months"),
        "benefits": item.get("benefits") or [],
        "source_url": str(source_url or "").strip().rstrip("/"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    inserted = client.insert_rows(TABLE_CLINIC_MEMBERSHIPS, [row])
    return {
        "accepted": True,
        "action": "inserted",
        "plan_id": int(inserted[0]["plan_id"]),
    }


def persist_promotion_item(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
) -> dict[str, Any]:
    expanded = dict(item)
    expanded["promotion_content"] = build_promotion_content(item, evidence)
    decision = validate_promotion(expanded, evidence)
    if not decision.accepted:
        return {"accepted": False, "reason": decision.reason, "action": "skipped"}
    promotion_id = upsert_promotion(client, business_id=business_id, source_url=source_url)
    client.update_row(
        TABLE_CLINIC_PROMOTIONS,
        {"promotion_id": f"eq.{promotion_id}"},
        {
            "promotion_title": str(expanded.get("promotion_title") or "Promotion"),
            "promotion_content": expanded.get("promotion_content") or [],
            "campaign_start_date": expanded.get("campaign_start_date"),
            "campaign_end_date": expanded.get("campaign_end_date"),
            "is_active": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return {"accepted": True, "action": "upserted", "promotion_id": int(promotion_id)}


def persist_promo_offer(
    client: Any,
    *,
    business_id: int,
    promotion_id: int,
    source_url: str,
    offer: dict[str, Any],
    membership_plan_id: Optional[int] = None,
    seen_fingerprints: set[str] | None = None,
) -> dict[str, Any]:
    if route_offer(offer) != "promo":
        return {"accepted": False, "reason": exclude_reason(offer) or "not_promo", "action": "skipped"}
    master, items = build_master_from_offer(
        offer,
        business_id=business_id,
        promotion_id=promotion_id,
        source_url=source_url,
        membership_plan_id=membership_plan_id,
    )
    fp = str(master.get("offer_fingerprint") or "")
    if seen_fingerprints is not None and fp and fp in seen_fingerprints:
        return {"accepted": False, "reason": "duplicate_fingerprint", "action": "skipped", "offer_fingerprint": fp}
    inserted = client.insert_rows(TABLE_PROMO_OFFER_MASTER, [master])
    offer_id = int(inserted[0]["id"])
    linked_items = attach_service_ids_to_items(
        client,
        business_id=business_id,
        items=items,
        fallback_text=str(offer.get("offer_raw_text") or ""),
    )
    upsert_offer_items(client, offer_id, linked_items)
    if seen_fingerprints is not None and fp:
        seen_fingerprints.add(fp)
    return {
        "accepted": True,
        "action": "inserted",
        "offer_id": offer_id,
        "items": len(linked_items),
        "offer_fingerprint": fp,
    }


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
        "quarantine": [],
    }
    service_candidates: list[dict[str, Any]] = []
    for offer in offers:
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
            stats["quarantine"].append(
                {
                    "reason": exclude_reason(offer) or "unroutable",
                    "offer_raw_text": str(offer.get("offer_raw_text") or "")[:200],
                }
            )
            continue
        write = persist_promo_offer(
            client,
            business_id=business_id,
            promotion_id=promotion_id,
            source_url=source_url,
            offer=offer,
            membership_plan_id=membership_plan_id,
            seen_fingerprints=seen_fingerprints,
        )
        stats["promo_writes"].append(write)
        if write.get("accepted"):
            stats["promos"] += 1
        else:
            stats["skipped"] += 1

    for item in pick_best_service_items(service_candidates, evidence):
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
