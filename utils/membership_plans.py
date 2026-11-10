"""Membership plan extraction and persistence helpers."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from utils.promo_offer_items_db import build_item_from_offer_fields
from utils.schema_contract import TABLE_CLINIC_MEMBERSHIPS

_VALID_BILLING_PERIODS = {"monthly", "annual", "weekly"}
_PER_UNIT_OFFER_SIGNAL = re.compile(
    r"(?:/|per)\s*(?:u(?:nit)?s?)\b|\ba\s+unit\b",
    re.IGNORECASE,
)


def _parse_fee(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def normalize_membership_plan(record: Dict[str, Any]) -> Dict[str, Any]:
    tier_name = str(record.get("tier_name") or record.get("plan_name") or "Standard").strip()
    plan_name = str(record.get("plan_name") or tier_name).strip()
    billing_period = str(record.get("billing_period") or "monthly").strip().lower()
    if billing_period not in _VALID_BILLING_PERIODS:
        billing_period = "monthly"

    benefits = record.get("benefits")
    if benefits is not None and not isinstance(benefits, list):
        benefits = [benefits] if benefits else None
    source_claims = record.get("source_claims") or []
    if not isinstance(source_claims, list) or any(not isinstance(value, str) for value in source_claims):
        raise ValueError(f"membership source_claims must be a string array: {plan_name}")

    priced_offers = record.get("priced_offers") or []
    if not isinstance(priced_offers, list):
        priced_offers = []

    return {
        "tier_name": tier_name,
        "plan_name": plan_name,
        "monthly_fee": _parse_fee(record.get("monthly_fee")),
        "annual_fee": _parse_fee(record.get("annual_fee")),
        "billing_period": billing_period,
        "benefits": benefits,
        "source_claims": [value.strip() for value in source_claims if value.strip()],
        "priced_offers": [item for item in priced_offers if isinstance(item, dict)],
    }


def normalize_membership_payload(payload: Any) -> List[Dict[str, Any]]:
    from utils.offer_extraction_llm import parse_json_payload

    data = parse_json_payload(payload, {})
    plans = data.get("membership_plans", []) if isinstance(data, dict) else []
    if not isinstance(plans, list):
        return []
    return [normalize_membership_plan(item) for item in plans if isinstance(item, dict)]


def _membership_price(plan: Dict[str, Any]) -> Optional[float]:
    billing = str(plan.get("billing_period") or "monthly").lower()
    if billing == "annual" and plan.get("annual_fee") is not None:
        return _parse_fee(plan["annual_fee"])
    if plan.get("monthly_fee") is not None:
        return _parse_fee(plan["monthly_fee"])
    return _parse_fee(plan.get("annual_fee"))


def _benefits_list(benefits: Any) -> Optional[list]:
    if benefits is None:
        return None
    if isinstance(benefits, str):
        try:
            parsed = json.loads(benefits)
            benefits = parsed if isinstance(parsed, list) else [benefits]
        except (TypeError, ValueError, json.JSONDecodeError):
            benefits = [benefits]
    if not isinstance(benefits, list):
        benefits = [benefits]
    values = [str(item.get("desc") or "") if isinstance(item, dict) else str(item) for item in benefits]
    return [value.strip() for value in values if value.strip()]


def build_membership_plan_insert_row(plan: Dict[str, Any], staging_row: Dict[str, Any]) -> Dict[str, Any]:
    tier_name = str(plan.get("tier_name") or plan.get("plan_name") or "Standard").strip()
    plan_name = str(plan.get("plan_name") or tier_name).strip()
    price = _membership_price(plan)
    if price is None:
        raise ValueError(f"membership plan missing price: {plan_name}")
    row: Dict[str, Any] = {
        "membership_name": plan_name,
        "membership_price": price,
        "billing_period": plan["billing_period"],
        "benefits": _benefits_list(plan.get("benefits")),
        "source_claims": plan.get("source_claims") or [],
    }
    if staging_row.get("business_id") is not None:
        row["business_id"] = staging_row["business_id"]
    source_url = str(staging_row.get("subpage_url") or staging_row.get("source_url") or "").strip().rstrip("/")
    if source_url:
        row["source_url"] = source_url
    if plan.get("minimum_commitment_months") is not None:
        row["minimum_commitment_months"] = plan["minimum_commitment_months"]
    return row


def build_priced_offer_insert_row(
    priced_offer: Dict[str, Any],
    *,
    membership_plan_id: int,
    staging_row: Dict[str, Any],
) -> Dict[str, Any]:
    from utils.offer_extraction_llm import canonicalize_service_name

    service_name = canonicalize_service_name(
        priced_offer.get("service_name"),
        priced_offer.get("offer_raw_text"),
    )
    price = _parse_fee(priced_offer.get("price") or priced_offer.get("discount_price"))
    regular_price = _parse_fee(priced_offer.get("regular_price") or priced_offer.get("original_price"))
    unit_type = str(priced_offer.get("unit_type") or "").strip()
    offer_raw_text = str(priced_offer.get("offer_raw_text") or "").strip()
    if not offer_raw_text:
        parts = [service_name]
        if price is not None:
            parts.append(f"${price:g}")
        if unit_type:
            parts.append(f"per {unit_type}")
        offer_raw_text = " ".join(parts)
    price_model = (
        "per_unit"
        if unit_type.casefold() in {"unit", "units", "per_unit"}
        or _PER_UNIT_OFFER_SIGNAL.search(offer_raw_text)
        else "total"
    )

    row: Dict[str, Any] = {
        "is_active": False,
        "is_new_customer_required": False,
        "offer_raw_text": offer_raw_text,
        "membership_plan_id": membership_plan_id,
        "price_model": price_model,
    }
    if staging_row.get("business_id") is not None:
        row["business_id"] = staging_row["business_id"]
    if price is not None:
        row["discount_price"] = price
    if regular_price is not None:
        row["regular_price"] = regular_price
    if priced_offer.get("members_only"):
        row["is_membership_required"] = True
    row["_item"] = build_item_from_offer_fields(
        {"service_name": service_name, "unit_type": unit_type},
    )
    if price_model == "per_unit" and price is not None:
        row["_item"]["unit_price"] = price
    return row


def membership_offer_fingerprint(
    *,
    membership_plan_id: int,
    service_name: str,
    unit_type: str,
    discount_price: Any,
) -> str:
    price = _parse_fee(discount_price)
    return "|".join(
        [
            str(membership_plan_id),
            service_name.lower(),
            unit_type.lower(),
            "" if price is None else f"{price:g}",
        ]
    )


def _is_pure_membership_offer(offer: Dict[str, Any]) -> bool:
    service_name = str(offer.get("service_name") or "").lower()
    offer_raw = str(offer.get("offer_raw_text") or "").lower()
    if "member" in service_name or "membership" in service_name:
        return True
    if re.search(r"/\s*mo\b|/month|monthly", offer_raw):
        if not re.search(r"\b(botox|filler|tox|unit|syringe)\b", offer_raw):
            return True
    return False


def infer_tier_name_from_offer(offer: Dict[str, Any]) -> str:
    raw = str(offer.get("raw_service_name") or "").strip()
    if raw and raw.lower() != "membership":
        return raw
    membership_name = str(offer.get("membership_name") or "").strip()
    if membership_name:
        return membership_name
    offer_content = offer.get("offer_content")
    content_text = ""
    if isinstance(offer_content, str):
        content_text = offer_content.strip()
    text = " ".join(
        part for part in (str(offer.get("offer_raw_text") or "").strip(), content_text) if part
    )
    cleaned = re.sub(
        r"\$\s*\d+(?:,\d{3})*(?:\.\d+)?(?:\s*/\s*(?:mo|month|yr|year|annual|week))?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:per\s+month|per\s+year|monthly|annual|yearly|weekly)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–|")
    if cleaned and cleaned.lower() not in {"membership", "member"}:
        return cleaned
    return "Standard"


def infer_billing_period_from_offer(offer: Dict[str, Any]) -> str:
    text = " ".join(
        str(offer.get(field) or "")
        for field in ("offer_raw_text", "billing_period", "offer_content")
        if not isinstance(offer.get(field), (dict, list))
    ).lower()
    if re.search(r"/\s*(?:yr|year|annual)\b|\b(?:annual|yearly)\b", text):
        return "annual"
    if re.search(r"/\s*week\b|\bweekly\b", text):
        return "weekly"
    return "monthly"


def infer_plan_fees_from_offer(
    offer: Dict[str, Any],
) -> tuple[Optional[float], Optional[float], str]:
    text = str(offer.get("offer_raw_text") or "")
    billing = infer_billing_period_from_offer(offer)
    monthly: Optional[float] = None
    annual: Optional[float] = None

    for pattern in (
        r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*(?:mo|month)\b",
        r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:per\s+month|monthly)\b",
        r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*month\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            monthly = _parse_fee(match.group(1))
            break

    for pattern in (
        r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*(?:yr|year|annual)\b",
        r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:per\s+year|annual)\b",
        r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*year\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            annual = _parse_fee(match.group(1))
            break

    if monthly is None and annual is None:
        fee = _parse_fee(offer.get("discount_price"))
        if fee is None:
            fee = _parse_fee(offer.get("membership_price"))
        if fee is not None:
            if billing == "annual":
                annual = fee
            else:
                monthly = fee

    return monthly, annual, billing


def offer_row_to_membership_plan(offer: Dict[str, Any]) -> Dict[str, Any]:
    tier_name = infer_tier_name_from_offer(offer)
    monthly_fee, annual_fee, billing_period = infer_plan_fees_from_offer(offer)
    if monthly_fee is not None:
        plan_name = f"{tier_name} ${monthly_fee:g}/month"
    elif annual_fee is not None:
        plan_name = f"{tier_name} ${annual_fee:g}/year"
    else:
        plan_name = tier_name
    return normalize_membership_plan(
        {
            "tier_name": tier_name,
            "plan_name": plan_name,
            "monthly_fee": monthly_fee,
            "annual_fee": annual_fee,
            "billing_period": billing_period,
            "benefits": None,
            "priced_offers": [],
        }
    )


def can_migrate_offer_to_plan(offer: Dict[str, Any]) -> bool:
    plan = offer_row_to_membership_plan(offer)
    return plan.get("monthly_fee") is not None or plan.get("annual_fee") is not None


def find_existing_plan_id(
    client: Any,
    business_id: int,
    membership_name: str,
) -> Optional[int]:
    from utils.membership_plan_lookup import normalize_plan_name

    name_norm = normalize_plan_name(membership_name)
    if not name_norm:
        return None
    rows = client.fetch_rows(
        TABLE_CLINIC_MEMBERSHIPS,
        "plan_id,membership_name",
        filters={"business_id": f"eq.{business_id}"},
        limit=200,
    )
    for row in rows:
        if normalize_plan_name(row.get("membership_name")) == name_norm:
            return int(row["plan_id"])
    return None
