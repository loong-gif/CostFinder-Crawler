"""Repair planners for promo_offer_master QA governance."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from utils.membership_plans import (
    build_membership_plan_insert_row_from_offer,
    can_migrate_offer_to_plan,
    find_existing_plan_id,
    offer_row_to_membership_plan,
)
from utils.offer_fingerprint import normalize_unit_type
from utils.offer_scope_filter import exclude_reason, is_consultation_offer, is_membership_plan_offer
from utils.promo_offer_audit import is_high_confidence_skincare_product, parse_float
from utils.schema_contract import offer_is_active, offer_item_name, offer_source_url
from utils.service_category_lookup import resolve_service_category

_TREATMENT_IN_RAW = re.compile(
    r"\b(botox|dysport|filler|tox|unit|syringe|juvederm|xeomin|jeuveau|daxxify|sculptra|kybella|hydrafacial|laser|microneedling|prf|prp)\b",
    re.IGNORECASE,
)
_PRICE_PAIR_RE = re.compile(
    r"(?:regular|was|original|list)\s*(?:price)?\s*[:~]?\s*\$?\s*(\d+(?:\.\d+)?).{0,80}?(?:sale|now|discount|member)\s*(?:price)?\s*[:~]?\s*\$?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)
_SWAP_PAIR_RE = re.compile(
    r"\$?\s*(\d+(?:\.\d+)?)\s*(?:sale|now|member)\s*(?:price)?.*?\$?\s*(\d+(?:\.\d+)?)\s*(?:regular|was|original)",
    re.IGNORECASE | re.DOTALL,
)


def _domain(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        return urlparse(raw).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _norm_url(value: Any) -> str:
    return str(value or "").strip().lower().rstrip("/")


def build_business_lookup(
    master_rows: Sequence[Mapping[str, Any]],
    staging_rows: Sequence[Mapping[str, Any]],
    promotion_rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Tuple[Dict[str, Set[Any]], Dict[str, Set[Any]]]:
    """url -> business_ids, domain -> business_ids."""
    url_map: Dict[str, Set[Any]] = defaultdict(set)
    dom_map: Dict[str, Set[Any]] = defaultdict(set)
    for row in promotion_rows or ():
        bid = row.get("business_id")
        if bid is None:
            continue
        url_map[_norm_url(row.get("source_url"))].add(bid)
        dom = _domain(row.get("source_url"))
        if dom:
            dom_map[dom].add(bid)
    for row in staging_rows:
        bid = row.get("business_id")
        if bid is None:
            continue
        url_map[_norm_url(row.get("subpage_url"))].add(bid)
        dom = _domain(row.get("subpage_url")) or _domain(row.get("domain_name"))
        if dom:
            dom_map[dom].add(bid)
    for row in master_rows:
        bid = row.get("business_id")
        if bid is None:
            continue
        dom = _domain(row.get("website"))
        if dom:
            dom_map[dom].add(bid)
    return url_map, dom_map


def resolve_business_id(
    offer: Mapping[str, Any],
    *,
    url_map: Mapping[str, Set[Any]],
    dom_map: Mapping[str, Set[Any]],
) -> Tuple[Optional[Any], str]:
    if offer.get("business_id") is not None:
        return offer["business_id"], "already_set"
    source_url = _norm_url(offer_source_url(dict(offer)))
    ids = url_map.get(source_url) or set()
    if len(ids) == 1:
        return next(iter(ids)), "exact_source_url"
    dom = _domain(source_url) or _domain(offer.get("source_name"))
    ids = dom_map.get(dom) or set()
    if len(ids) == 1:
        return next(iter(ids)), "unique_domain"
    if len(ids) > 1:
        return None, "ambiguous_domain"
    return None, "unresolved"


def should_swap_prices(offer: Mapping[str, Any]) -> bool:
    regular = parse_float(offer.get("regular_price"))
    discount = parse_float(offer.get("discount_price"))
    if regular is None or discount is None or discount <= regular:
        return False
    raw = str(offer.get("offer_raw_text") or "")
    if _SWAP_PAIR_RE.search(raw):
        return True
    match = _PRICE_PAIR_RE.search(raw)
    if match:
        text_regular = parse_float(match.group(1))
        text_discount = parse_float(match.group(2))
        if text_regular is not None and text_discount is not None:
            return abs(text_regular - discount) < 0.01 and abs(text_discount - regular) < 0.01
    return False


def infer_unit_type_from_text(offer: Mapping[str, Any]) -> Optional[str]:
    raw = str(offer.get("offer_raw_text") or "")
    service = offer_item_name(dict(offer))
    text = f"{service} {raw}"
    if re.search(r"(?:/|per)\s*unit\b|\bper unit\b|\b\d+\s*units?\b", text, re.I):
        return "unit"
    if re.search(r"(?:/|per)\s*(?:syringe|half syringe)\b|\bhalf syringe\b", text, re.I):
        return "half syringe" if "half syringe" in text.lower() else "syringe"
    if re.search(r"(?:/|per)\s*session\b|\bsessions?\b", text, re.I):
        return "session"
    if re.search(r"(?:/|per)\s*vial\b|\bvials?\b", text, re.I):
        return "vial"
    if re.search(r"(?:/|per)\s*area\b|\bareas?\b", text, re.I):
        return "area"
    return None


def plan_p0_repairs(rows: Sequence[Mapping[str, Any]], *, today: Optional[date] = None) -> List[Dict[str, Any]]:
    today = today or date.today()
    actions: List[Dict[str, Any]] = []
    for row in rows:
        if not offer_is_active(dict(row)):
            continue
        row_id = row.get("id")
        end_date = str(row.get("end_date") or "").strip()
        if end_date and end_date < today.isoformat():
            actions.append(
                {
                    "batch": "p0",
                    "action": "update",
                    "id": row_id,
                    "fields": {"is_active": False},
                    "reason": f"active_past_end_date={end_date}",
                }
            )
            continue
        if should_swap_prices(row):
            actions.append(
                {
                    "batch": "p0",
                    "action": "update",
                    "id": row_id,
                    "fields": {
                        "regular_price": parse_float(row.get("discount_price")),
                        "discount_price": parse_float(row.get("regular_price")),
                    },
                    "reason": "swap_inverted_prices_from_text",
                }
            )
        if not offer_item_name(dict(row)) and not str(row.get("offer_raw_text") or "").strip():
            actions.append(
                {
                    "batch": "p0",
                    "action": "update",
                    "id": row_id,
                    "fields": {"is_active": False},
                    "reason": "missing_item_name",
                }
            )
        for field in ("regular_price", "discount_price"):
            if parse_float(row.get(field)) == 0 and is_consultation_offer(dict(row)):
                actions.append(
                    {
                        "batch": "p0",
                        "action": "delete",
                        "id": row_id,
                        "reason": f"zero_price_consultation_{field}",
                    }
                )
                break
    return actions


def plan_business_repairs(
    rows: Sequence[Mapping[str, Any]],
    *,
    url_map: Mapping[str, Set[Any]],
    dom_map: Mapping[str, Set[Any]],
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for row in rows:
        if not offer_is_active(dict(row)) or row.get("business_id") is not None:
            continue
        bid, method = resolve_business_id(row, url_map=url_map, dom_map=dom_map)
        if bid is not None:
            actions.append(
                {
                    "batch": "business",
                    "action": "update",
                    "id": row.get("id"),
                    "fields": {"business_id": bid},
                    "reason": f"backfill_business_id:{method}",
                }
            )
        else:
            actions.append(
                {
                    "batch": "business",
                    "action": "update",
                    "id": row.get("id"),
                    "fields": {"is_active": False},
                    "reason": f"unresolved_business_id:{method}",
                }
            )
    return actions


def plan_membership_repairs(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("membership_plan_id"):
            continue
        if not is_membership_plan_offer(dict(row)):
            continue
        row_id = row.get("id")
        if can_migrate_offer_to_plan(dict(row)):
            plan = offer_row_to_membership_plan(dict(row))
            actions.append(
                {
                    "batch": "membership",
                    "action": "migrate_plan_then_delete_offer",
                    "id": row_id,
                    "plan_preview": plan,
                    "reason": "pure_membership_migrate_to_plans",
                }
            )
        else:
            actions.append(
                {
                    "batch": "membership",
                    "action": "delete",
                    "id": row_id,
                    "reason": "pure_membership_no_reliable_fee",
                }
            )
    return actions


def plan_consultation_repairs(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "batch": "consultation",
            "action": "delete",
            "id": row.get("id"),
            "reason": "consultation_not_treatment_offer",
        }
        for row in rows
        if is_consultation_offer(dict(row))
    ]


def plan_retail_repairs(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "batch": "retail",
            "action": "delete",
            "id": row.get("id"),
            "reason": "high_confidence_skincare_product",
        }
        for row in rows
        if is_high_confidence_skincare_product(row)
    ]


def plan_category_repairs(
    rows: Sequence[Mapping[str, Any]],
    *,
    sibling_index: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for row in rows:
        if not offer_is_active(dict(row)):
            continue
        if str(row.get("service_category") or "").strip():
            continue
        category, method, confidence = resolve_service_category(
            offer_item_name(dict(row)),
            "",
            sibling_index=sibling_index,
            min_confidence="medium",
        )
        if not category:
            continue
        actions.append(
            {
                "batch": "category",
                "action": "update",
                "id": row.get("id"),
                "fields": {"service_category": category},
                "reason": f"backfill_category:{method}:{confidence}",
            }
        )
    return actions


def plan_unit_repairs(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for row in rows:
        if not offer_is_active(dict(row)):
            continue
        current = normalize_unit_type(row.get("unit_type"))
        inferred = infer_unit_type_from_text(row)
        if not inferred:
            continue
        if current == inferred:
            continue
        if current and current not in {"units", "unit", ""}:
            continue
        actions.append(
            {
                "batch": "unit",
                "action": "update",
                "id": row.get("id"),
                "fields": {"unit_type": inferred},
                "reason": f"normalize_unit_type:{current or '<empty>'}->{inferred}",
            }
        )
    return actions


def build_all_repair_plans(
    rows: Sequence[Mapping[str, Any]],
    *,
    master_rows: Sequence[Mapping[str, Any]],
    staging_rows: Sequence[Mapping[str, Any]],
    promotion_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    sibling_index: Optional[Mapping[str, str]] = None,
    today: Optional[date] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    url_map, dom_map = build_business_lookup(
        master_rows, staging_rows, promotion_rows=promotion_rows
    )
    return {
        "p0": plan_p0_repairs(rows, today=today),
        "business": plan_business_repairs(rows, url_map=url_map, dom_map=dom_map),
        "membership": plan_membership_repairs(rows),
        "consultation": plan_consultation_repairs(rows),
        "retail": plan_retail_repairs(rows),
        "category": plan_category_repairs(rows, sibling_index=sibling_index),
        "unit": plan_unit_repairs(rows),
    }


def membership_plan_row_for_offer(
    offer: Mapping[str, Any],
    staging_row: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return build_membership_plan_insert_row_from_offer(dict(offer), staging_row)
