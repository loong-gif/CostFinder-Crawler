"""Persist LLM service extraction rows into clinic_services."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from utils.clinic_services_db import fetch_service_row
from utils.db_rows import ClinicServiceInsertRow
from utils.recent_raw_extraction import validate_service
from utils.schema_contract import TABLE_CLINIC_SERVICES

_VALID_CATEGORIES = frozenset({"Neurotoxin", "Filler", "others"})
_VALID_UNIT_TYPES = frozenset(
    {
        "unit",
        "syringe",
        "half_syringe",
        "vial",
        "treatment",
        "session",
        "package",
        "area",
        "ml",
        "mg",
        "others",
    }
)


def _normalize_category(value: Any) -> str:
    category = str(value or "others").strip() or "others"
    return category if category in _VALID_CATEGORIES else "others"


def _normalize_unit_type(value: Any) -> str:
    unit_type = str(value or "others").strip() or "others"
    return unit_type if unit_type in _VALID_UNIT_TYPES else "others"


def _positive_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def offer_discount_price(offer: dict[str, Any]) -> float | None:
    return _positive_number(offer.get("discount_price"))


def offer_discount_percent(offer: dict[str, Any]) -> float | None:
    return _positive_number(offer.get("discount_percent"))


def offer_discount_amount(offer: dict[str, Any]) -> float | None:
    return _positive_number(offer.get("discount_amount"))


def is_promo_offer(offer: dict[str, Any]) -> bool:
    return (
        offer_discount_price(offer) is not None
        or offer_discount_percent(offer) is not None
        or offer_discount_amount(offer) is not None
    )


def offer_to_clinic_service_item(offer: dict[str, Any]) -> dict[str, Any] | None:
    """Map a list-price offer row to a clinic_services upsert payload."""
    value = offer.get("regular_price")
    if value is None:
        return None
    try:
        regular_price = float(value)
    except (TypeError, ValueError):
        return None
    if regular_price <= 0:
        return None

    items = offer.get("items") or []
    first = items[0] if items else {}
    from utils.offer_extraction_llm import canonicalize_service_name

    raw_text = str(offer.get("offer_raw_text") or "")
    service_name = (
        canonicalize_service_name(
            first.get("service_name"),
            first.get("service_area"),
            raw_text,
        )
        or "Others"
    )
    raw_name = str(first.get("service_name") or raw_text or service_name).strip()
    item: dict[str, Any] = {
        "service_name": service_name,
        "service_name_raw": raw_name,
        "regular_price": regular_price,
        "unit_type": first.get("unit_type") or "others",
    }
    if first.get("service_category") is not None:
        item["service_category"] = first.get("service_category")
    if first.get("service_area") is not None:
        item["service_area"] = first.get("service_area")
    return item


_UNIT_TYPE_RANK = {
    "unit": 0,
    "syringe": 1,
    "half_syringe": 2,
    "vial": 3,
    "session": 4,
    "treatment": 5,
    "area": 6,
    "package": 7,
    "ml": 8,
    "mg": 9,
    "others": 10,
}


def _unit_type_rank(value: Any) -> int:
    unit_type = _normalize_unit_type(value)
    return _UNIT_TYPE_RANK.get(unit_type, 10)


def pick_best_service_items(items: list[dict[str, Any]], evidence: str) -> list[dict[str, Any]]:
    """Keep one row per canonical service_name; prefer unit/syringe/vial list prices over packages."""
    best: dict[str, dict[str, Any]] = {}
    for item in items:
        decision = validate_service(item, evidence)
        if not decision.accepted:
            continue
        std_name = str(item.get("service_name") or "Others").strip() or "Others"
        current = best.get(std_name)
        if current is None:
            best[std_name] = item
            continue
        candidate_rank = _unit_type_rank(item.get("unit_type"))
        current_rank = _unit_type_rank(current.get("unit_type"))
        if candidate_rank < current_rank:
            best[std_name] = item
            continue
        if candidate_rank > current_rank:
            continue
        try:
            candidate_price = float(item.get("regular_price"))
            current_price = float(current.get("regular_price"))
        except (TypeError, ValueError):
            continue
        if candidate_price < current_price:
            best[std_name] = item
    return list(best.values())


def attach_service_ids_to_items(
    client: Any,
    *,
    business_id: int,
    items: list[dict[str, Any]],
    fallback_text: str = "",
) -> list[dict[str, Any]]:
    from utils.offer_extraction_llm import canonicalize_service_name

    attached: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        name = canonicalize_service_name(
            row.get("service_name"),
            row.get("item_name"),
            fallback_text,
        )
        svc = fetch_service_row(client, business_id, name)
        if svc:
            row["service_id"] = int(svc["service_id"])
        attached.append(row)
    return attached


def upsert_extracted_service(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
) -> dict[str, Any]:
    decision = validate_service(item, evidence)
    std_name = str(item.get("service_name") or "Others").strip() or "Others"
    result = {
        "business_id": business_id,
        "service_name": std_name,
        "source_url": str(source_url or "").strip().rstrip("/"),
        "accepted": decision.accepted,
        "reason": decision.reason,
        "service_id": None,
        "action": "skipped",
    }
    if not decision.accepted:
        return result

    price = item.get("regular_price")
    unit_type = _normalize_unit_type(item.get("unit_type"))
    category = _normalize_category(item.get("service_category"))
    raw_name = str(item.get("service_name_raw") or "").strip()

    existing = fetch_service_row(client, business_id, std_name)
    if existing:
        service_id = int(existing["service_id"])
    else:
        insert_row: dict[str, Any] = {
            "business_id": business_id,
            "service_name": std_name,
            "regular_price": float(price),
            "unit_type": unit_type,
            "service_category": category,
            "source_url": result["source_url"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if raw_name:
            insert_row["service_name_raw"] = raw_name
        area = item.get("service_area")
        if area is not None and str(area).strip():
            insert_row["service_area"] = str(area).strip()
        try:
            insert_payload = ClinicServiceInsertRow.model_validate(insert_row).to_api_dict()
        except ValidationError:
            return {**result, "accepted": False, "reason": "schema_invalid", "action": "skipped"}
        inserted = client.insert_rows(TABLE_CLINIC_SERVICES, [insert_payload])
        service_id = int(inserted[0]["service_id"])
        result.update({"service_id": service_id, "action": "inserted"})
        return result

    payload: dict[str, Any] = {
        "source_url": result["source_url"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if raw_name:
        payload["service_name_raw"] = raw_name
    if item.get("service_category") is not None:
        payload["service_category"] = category
    if price is not None:
        payload["regular_price"] = float(price)
    if str(item.get("unit_type") or "").strip():
        payload["unit_type"] = unit_type
    area = item.get("service_area")
    if area is not None and str(area).strip():
        payload["service_area"] = str(area).strip()

    client.update_row(
        TABLE_CLINIC_SERVICES,
        {"service_id": f"eq.{service_id}"},
        payload,
    )
    result.update({"service_id": service_id, "action": "updated"})
    return result
