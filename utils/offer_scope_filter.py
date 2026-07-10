"""Exclude consultation and membership-plan rows from promo_offer_master."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from utils.membership_plans import _is_pure_membership_offer
from utils.skincare_products import is_skincare_product_offer

_CONSULTATION_RE = re.compile(r"\bconsultation\b", re.IGNORECASE)
_FREE_ZERO_RE = re.compile(r"\bfree\b|\$0(?:\.\d+)?\b|0\.00", re.IGNORECASE)
_SERVICE_UNIT_RE = re.compile(
    r"\b(botox|dysport|filler|tox|unit|syringe|juvederm|xeomin|jeuveau|daxxify|sculptra|kybella|hydrafacial|laser|microneedling)\b",
    re.IGNORECASE,
)


def _text_blob(offer: Dict[str, Any]) -> str:
    parts = [
        offer.get("service_name"),
        offer.get("raw_service_name"),
        offer.get("display_service_name"),
        offer.get("offer_raw_text"),
    ]
    content = offer.get("offer_content")
    if isinstance(content, str):
        parts.append(content)
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def is_consultation_offer(offer: Dict[str, Any]) -> bool:
    service = str(offer.get("service_name") or "").strip().lower()
    if service in {"free consultation", "consultation"}:
        return True
    if "consultation" in service.replace("_", " "):
        return True
    if _CONSULTATION_RE.search(str(offer.get("service_name") or "")):
        return True
    if _CONSULTATION_RE.search(str(offer.get("raw_service_name") or "")):
        return True
    if _CONSULTATION_RE.search(str(offer.get("display_service_name") or "")):
        return True
    text = _text_blob(offer)
    raw = str(offer.get("offer_raw_text") or "")
    if _CONSULTATION_RE.search(raw) and _FREE_ZERO_RE.search(raw):
        if not re.search(r"\$\s*\d+(?:\.\d+)?\s*/\s*(?:unit|syringe|area)\b", raw, re.IGNORECASE):
            return True
    if not _CONSULTATION_RE.search(text):
        return False
    if _FREE_ZERO_RE.search(text) and not _SERVICE_UNIT_RE.search(text):
        return True
    if service in {"", "others"} and _CONSULTATION_RE.search(text):
        return True
    return False


def is_membership_plan_offer(offer: Dict[str, Any]) -> bool:
    """Membership tier/plan fee — not a concrete treatment SKU price."""
    service = str(offer.get("service_name") or "").strip()
    if service == "Membership":
        return True
    if offer.get("membership_plan_id"):
        return False
    template = str(offer.get("template_type") or "").strip().lower()
    if template == "membership":
        return True
    return _is_pure_membership_offer(offer)


def should_exclude_from_offer_master(offer: Dict[str, Any]) -> bool:
    return (
        is_consultation_offer(offer)
        or is_membership_plan_offer(offer)
        or is_skincare_product_offer(offer)
    )


def exclude_reason(offer: Dict[str, Any]) -> str:
    if is_consultation_offer(offer):
        return "consultation"
    if is_membership_plan_offer(offer):
        return "membership_plan"
    if is_skincare_product_offer(offer):
        return "skincare_product"
    return ""


def filter_service_offers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [offer for offer in offers if not should_exclude_from_offer_master(offer)]
