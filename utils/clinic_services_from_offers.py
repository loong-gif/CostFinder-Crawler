"""Map promo_offer_master Botox rows into clinic_services catalog fields."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence

from utils.clinic_services_botox import BotoxServiceFields
from utils.offer_field_normalize import normalize_service_area
from utils.offer_fingerprint import normalize_unit_type
from utils.schema_contract import offer_is_active, offer_item_name, offer_source_url, offer_unit_type

BOTOX_SERVICE_NAME = "Botox"
_VALID_UNIT_TYPES = frozenset({"unit", "syringe", "area", "vial"})
_PER_UNIT_TEXT_RE = re.compile(
    r"\$\s*\d+(?:\.\d+)?\s*(?:/|per)\s*(?:unit|units|syringe|area|vial)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClinicServiceFromOfferFields:
    regular_price: Optional[Decimal]
    unit_type: Optional[str]
    service_area: Optional[str]


def flatten_offer_row(offer: Dict[str, Any]) -> Dict[str, Any]:
    """Expose item/promotion fields on the offer dict for downstream helpers."""
    flat = dict(offer)
    flat.setdefault("service_name", offer_item_name(flat))
    flat.setdefault("unit_type", offer_unit_type(flat))
    flat.setdefault("source_url", offer_source_url(flat))
    if "is_active" not in flat and "status" in flat:
        flat["is_active"] = str(flat.get("status") or "").lower() == "active"
    return flat


def _parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0 or amount > Decimal("1000"):
        return None
    return amount


def _is_truthy_package(offer: Dict[str, Any]) -> bool:
    raw = offer.get("is_package")
    if raw is True:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "1", "yes"}
    return False


def _has_unit_semantics(offer: Dict[str, Any]) -> bool:
    row = flatten_offer_row(offer)
    unit = normalize_unit_type(row.get("unit_type"))
    if unit in _VALID_UNIT_TYPES:
        return True
    blob = " ".join(
        str(row.get(key) or "")
        for key in ("offer_raw_text", "source_url", "service_name")
    )
    return bool(_PER_UNIT_TEXT_RE.search(blob))


def _offer_score(offer: Dict[str, Any]) -> tuple:
    row = flatten_offer_row(offer)
    regular = _parse_decimal(row.get("regular_price"))
    unit = normalize_unit_type(row.get("unit_type"))
    has_unit_price = bool(regular and unit in _VALID_UNIT_TYPES)
    has_regular = bool(regular)
    is_active = offer_is_active(row)
    is_package = _is_truthy_package(row)
    has_discount = _parse_decimal(row.get("discount_price")) is not None
    text_len = len(str(row.get("offer_raw_text") or ""))
    offer_id = int(row.get("id") or 0)
    return (
        1 if has_unit_price else 0,
        1 if is_active else 0,
        0 if is_package else 1,
        1 if has_discount and has_regular else 0,
        1 if has_regular else 0,
        text_len,
        offer_id,
    )


def pick_winner_botox_offer(offers: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Choose one Botox offer per business_id for catalog seeding."""
    candidates = [
        flatten_offer_row(offer)
        for offer in offers
        if str(offer_item_name(flatten_offer_row(offer)) or "").strip() == BOTOX_SERVICE_NAME
    ]
    if not candidates:
        return None
    return max(candidates, key=_offer_score)


def offer_to_clinic_fields(offer: Dict[str, Any]) -> ClinicServiceFromOfferFields:
    """Map offer columns to clinic_services; never uses discount_price."""
    from utils.service_price_guard import is_catalog_ineligible_url

    row = flatten_offer_row(offer)
    if is_catalog_ineligible_url(str(row.get("source_url") or "")):
        return ClinicServiceFromOfferFields(
            regular_price=None,
            unit_type=None,
            service_area=None,
        )
    unit_type = normalize_unit_type(row.get("unit_type")) or None
    service_area = normalize_service_area(row.get("service_area")) if row.get("service_area") else None

    regular_price: Optional[Decimal] = None
    parsed_regular = _parse_decimal(row.get("regular_price"))
    if parsed_regular is not None and _has_unit_semantics(row):
        regular_price = parsed_regular

    return ClinicServiceFromOfferFields(
        regular_price=regular_price,
        unit_type=unit_type,
        service_area=service_area,
    )


def to_botox_service_fields(fields: ClinicServiceFromOfferFields) -> BotoxServiceFields:
    return BotoxServiceFields(
        regular_price=fields.regular_price,
        unit_type=fields.unit_type,
        service_area=fields.service_area,
    )


def fields_have_updates(fields: ClinicServiceFromOfferFields) -> bool:
    return any(
        [
            fields.regular_price is not None,
            fields.unit_type,
            fields.service_area,
        ]
    )
