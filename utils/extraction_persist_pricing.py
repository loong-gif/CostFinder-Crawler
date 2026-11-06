from __future__ import annotations

import re
from typing import Any, Literal, Mapping, Optional

from utils.clinic_service_extraction import (
    is_promo_offer,
    offer_to_clinic_service_item,
)
from utils.offer_extraction_llm import canonicalize_service_name
from utils.offer_fingerprint import compute_offer_fingerprint
from utils.offer_scope_filter import should_exclude_from_offer_master
from utils.promo_offer_items_db import is_blanket_offer_text
from utils.service_price_guard import derive_offer_item_pricing

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
    if is_blanket_offer_text(str(offer.get("offer_raw_text") or "")) and is_promo_offer(offer):
        return "promo"
    if should_exclude_from_offer_master(offer):
        return "quarantine"
    if is_promo_offer(offer):
        return "promo"
    if offer_to_clinic_service_item(offer) is not None:
        return "service"
    return "quarantine"


def infer_price_model(offer: dict[str, Any]) -> str:
    raw = str(offer.get("offer_raw_text") or "").lower()
    if re.search(r"\b(?:starting\s+at|starts?\s+(?:at|from)|from)\s+\$", raw):
        return "from"
    # These signals describe the price semantics, not merely the presence of
    # a quantity.  A phrase such as "2 syringes for $900" remains a total.
    if re.search(
        r"(?:(?:/|per)\s*|a\s+)(?:u(?:nit)?s?|syringe|syringes|vial|vials|session|sessions|treatment|treatments)\b",
        raw,
    ):
        return "per_unit"
    if re.search(r"\b(?:starting\s+at|starts?\s+(?:at|from)|from)\s+\$", raw):
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
    raw_conditions = offer.get("conditions")
    if not isinstance(raw_conditions, list):
        raise ValueError("promo_offer_master requires verified conditions (use [] when none apply)")
    conditions: list[str] = []
    for condition in raw_conditions:
        if isinstance(condition, str):
            requirement = condition.strip()
        elif isinstance(condition, Mapping):
            # The extraction schema carries evidence until the shared gate
            # validates it; storage keeps only the requirement text.
            requirement_value = condition.get("requirement")
            evidence_value = condition.get("evidence")
            if not isinstance(requirement_value, str) or not isinstance(evidence_value, str) or not evidence_value.strip():
                raise ValueError("promo offer conditions require non-empty strings")
            requirement = requirement_value.strip()
        else:
            raise ValueError("promo offer conditions must be non-empty strings")
        if not requirement:
            raise ValueError("promo offer conditions require non-empty strings")
        conditions.append(requirement)
    items_in = offer.get("items") or []
    items_in = derive_offer_item_pricing(offer, evidence=str(offer.get("offer_raw_text") or ""))
    first = items_in[0] if items_in else {}
    is_pkg = offer.get("is_package") is True or offer.get("purchase_type") == "bundle"
    sibling_count = len(items_in)
    master_scope = str(offer.get("offer_raw_text") or "")
    if sibling_count > 1 or is_pkg:
        master_scope = str(first.get("item_name") or first.get("service_name") or master_scope)
    service_name = canonicalize_service_name(
        first.get("service_name"),
        first.get("service_area"),
        master_scope if sibling_count <= 1 and not is_pkg else first.get("item_name") or first.get("service_name"),
    ) or "Others"
    master: dict[str, Any] = {
        "business_id": business_id,
        "promotion_id": promotion_id,
        "is_active": True,
        "is_new_customer_required": offer.get("is_new_customer_required"),
        "is_membership_required": offer.get("is_membership_required"),
        "conditions": conditions,
        "offer_raw_text": str(offer.get("offer_raw_text") or service_name)[:4000],
        "price_model": infer_price_model(offer),
    }
    if offer.get("regular_price") is not None:
        master["regular_price"] = float(offer["regular_price"])
    if offer.get("discount_price") is not None:
        master["discount_price"] = float(offer["discount_price"])
    if master["price_model"] == "per_unit" and master.get("discount_price") is None:
        item_prices = [
            round(float(item["unit_price"]), 4)
            for item in items_in
            if item.get("unit_price") is not None
        ]
        if item_prices and len(item_prices) == len(items_in) and all(
            abs(price - item_prices[0]) <= 0.011 for price in item_prices
        ):
            master["discount_price"] = item_prices[0]
    if offer.get("discount_percent") is not None:
        master["discount_percent"] = float(offer["discount_percent"])
    if offer.get("discount_amount") is not None:
        master["discount_amount"] = float(offer["discount_amount"])
    if membership_plan_id is not None and offer.get("is_membership_required"):
        master["membership_plan_id"] = membership_plan_id
        master["promotion_id"] = None
    unit_type = str(first.get("unit_type") or "")
    raw_text = str(offer.get("offer_raw_text") or "")
    master["offer_fingerprint"] = compute_offer_fingerprint(
        source_url=source_url,
        service_name=service_name,
        unit_type=unit_type,
        regular_price=master.get("regular_price"),
        discount_price=master.get("discount_price"),
        offer_raw_text=raw_text,
        business_id=business_id,
        promotion_id=master.get("promotion_id"),
        membership_plan_id=master.get("membership_plan_id"),
        discount_percent=master.get("discount_percent"),
        discount_amount=master.get("discount_amount"),
    )
    item_rows: list[dict[str, Any]] = []
    for raw in items_in:
        source_item_name = str(
            raw.get("item_name")
            or raw.get("display_service_name")
            or raw.get("service_name")
            or "Offer item"
        ).strip()
        item_scope = raw_text
        if sibling_count > 1 or is_pkg:
            item_scope = str(raw.get("item_name") or raw.get("service_name") or "")
        name = canonicalize_service_name(
            raw.get("service_name"),
            raw.get("service_area"),
            item_scope,
        ) or str(raw.get("service_name") or "Offer item")
        # Keep the source-facing title separate from the canonical name used
        # for service matching. Unknown dictionary values must remain visible
        # instead of being rewritten to "Others".
        item: dict[str, Any] = {"item_name": source_item_name, "service_name": name}
        if raw.get("quantity") is not None:
            item["quantity"] = raw["quantity"]
        if raw.get("unit_price") is not None:
            item["unit_price"] = raw["unit_price"]
        item_rows.append(item)
    if not item_rows:
        item_rows = [{"service_name": service_name}]
    if is_blanket_offer_text(raw_text):
        item_rows = [{"item_name": str(first.get("item_name") or "").strip() or raw_text}]
    return master, item_rows
