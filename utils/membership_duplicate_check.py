"""Cross-source duplicate checks for promotion offers.

Membership offers are the canonical owner of benefits that are repeated on a
promotion page. This module only compares an already-normalized promotion
candidate with active membership offers for the same business; it does not
change offer identity or membership extraction.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from utils.offer_extraction_llm import canonicalize_service_name
from utils.offer_fingerprint import normalize_service_name
from utils.schema_contract import TABLE_PROMO_OFFER_MASTER


_PRICE_FIELDS = ("discount_price", "discount_percent", "discount_amount")
_MEMBERSHIP_OFFER_SELECT = (
    "id,business_id,membership_plan_id,discount_price,discount_percent,"
    "discount_amount,price_model,is_new_customer_required,conditions,"
    "promo_offer_items(item_name,service_id)"
)
_GENERIC_MEMBERSHIP_TERM = re.compile(
    r"^(?:(?:vip|member|members|membership|club|plan)(?:[- ]only)?"
    r"(?: pricing| benefit| benefits| offer| offers| discount|required)?)$"
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_item_identity(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    canonical = canonicalize_service_name(raw)
    if canonical and canonical != "Others":
        raw = canonical
    return normalize_service_name(raw)


def _item_names(items: Any, *, service_names: Mapping[int, str] | None = None) -> set[str]:
    if not isinstance(items, list):
        return set()
    names: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        # item_name is the persisted normalized identity. The service lookup
        # is only a fallback for older rows where it was not populated.
        name = str(item.get("item_name") or "").strip()
        if not name and service_names is not None and item.get("service_id") is not None:
            name = str(service_names.get(int(item["service_id"]), "")).strip()
        identity = _normalized_item_identity(name)
        if identity:
            names.add(identity)
    return names


def _candidate_item_names(items: Any) -> set[str]:
    if not isinstance(items, list):
        return set()
    names: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = item.get("item_name") or item.get("service_name")
        identity = _normalized_item_identity(name)
        if identity:
            names.add(identity)
    return names


def same_items(
    candidate_items: Any,
    existing_items: Any,
    *,
    service_names: Mapping[int, str] | None = None,
) -> bool:
    candidate_names = _candidate_item_names(candidate_items)
    existing_names = _item_names(existing_items, service_names=service_names)
    return bool(candidate_names) and candidate_names == existing_names


def same_offer_value(candidate: Mapping[str, Any], existing: Mapping[str, Any]) -> bool:
    """Compare every price field that either offer actually provides."""
    compared = False
    for field in _PRICE_FIELDS:
        candidate_value = _number(candidate.get(field))
        existing_value = _number(existing.get(field))
        if candidate_value is None and existing_value is None:
            continue
        compared = True
        if candidate_value is None or existing_value is None:
            return False
        if abs(candidate_value - existing_value) > 0.011:
            return False
    return compared


def _material_conditions(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    terms: set[str] = set()
    for raw in value:
        text = " ".join(str(raw or "").casefold().split()).strip(" .,:;-")
        if text and not _GENERIC_MEMBERSHIP_TERM.fullmatch(text):
            terms.add(text)
    return terms


def no_material_term_conflict(candidate: Mapping[str, Any], existing: Mapping[str, Any]) -> bool:
    if bool(candidate.get("is_new_customer_required")) != bool(existing.get("is_new_customer_required")):
        return False

    candidate_model = str(candidate.get("price_model") or "").strip()
    existing_model = str(existing.get("price_model") or "").strip()
    if candidate_model and existing_model and candidate_model != existing_model:
        return False

    candidate_terms = _material_conditions(candidate.get("conditions"))
    existing_terms = _material_conditions(existing.get("conditions"))
    # A promotion page often repeats only the price line and omits the
    # membership plan's own enrollment terms. That is not a conflict. When
    # both sources state material terms, they must agree.
    return not candidate_terms or not existing_terms or candidate_terms == existing_terms


def _service_name_fallbacks(client: Any, rows: list[dict[str, Any]], business_id: int) -> dict[int, str]:
    service_ids = {
        int(item["service_id"])
        for row in rows
        for item in row.get("promo_offer_items") or []
        if isinstance(item, Mapping)
        and not str(item.get("item_name") or "").strip()
        and item.get("service_id") is not None
    }
    if not service_ids:
        return {}
    services = client.fetch_rows(
        "clinic_services",
        "service_id,business_id,service_name",
        filters={
            "business_id": f"eq.{int(business_id)}",
            "service_id": f"in.({','.join(str(value) for value in sorted(service_ids))})",
        },
        limit=len(service_ids),
    )
    return {
        int(row["service_id"]): str(row.get("service_name") or "")
        for row in services
        if row.get("service_id") is not None
        and int(row.get("business_id") or business_id) == int(business_id)
    }


def find_matching_membership_offer(
    client: Any,
    *,
    business_id: int,
    candidate_master: Mapping[str, Any],
    candidate_items: Any,
) -> dict[str, Any] | None:
    """Return the canonical membership offer when the candidate is a duplicate."""
    rows = client.fetch_rows(
        TABLE_PROMO_OFFER_MASTER,
        _MEMBERSHIP_OFFER_SELECT,
        filters={
            "business_id": f"eq.{int(business_id)}",
            "membership_plan_id": "not.is.null",
            "is_active": "eq.true",
        },
        limit=1000,
        order="id.asc",
    )
    service_names = _service_name_fallbacks(client, rows, business_id)
    for existing in rows:
        if int(existing.get("business_id") or business_id) != int(business_id):
            continue
        if not same_items(candidate_items, existing.get("promo_offer_items"), service_names=service_names):
            continue
        if not same_offer_value(candidate_master, existing):
            continue
        if not no_material_term_conflict(candidate_master, existing):
            continue
        return existing
    return None
