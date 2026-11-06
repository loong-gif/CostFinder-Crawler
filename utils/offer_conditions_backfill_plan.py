from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from typing import Any

from utils.offer_conditions_backfill_matching import _extract_local_conditions
from utils.supabase_rest import SupabaseRestClient
from utils.supabase_access import fetch_all_rows


PAGE_SIZE = 1000
PLAN_VERSION = 1
OFFER_SELECT = (
    "id,business_id,promotion_id,membership_plan_id,offer_raw_text,regular_price,"
    "discount_price,discount_percent,discount_amount,is_membership_required,"
    "is_new_customer_required,updated_at,promo_offer_items(item_name,service_id,quantity:delivered_units,unit_price)"
)
PROMOTION_SELECT = "promotion_id,business_id,source_url,promotion_content,updated_at"
MEMBERSHIP_SELECT = (
    "plan_id,business_id,source_url,membership_name,minimum_commitment_months,benefits,updated_at"
)


def _fetch_all(
    client: SupabaseRestClient,
    table: str,
    select: str,
    *,
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    order = "id.asc" if table == "promo_offer_master" else None
    return fetch_all_rows(client, table, select, filters=filters, page_size=PAGE_SIZE, order=order)


def _in_filter(values: set[int]) -> str:
    return f"in.({','.join(str(value) for value in sorted(values))})"


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value in (None, [], {}):
        return ""
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _json_text(item)))
    return json.dumps(value, ensure_ascii=False)


def _source_text(source_type: str, source: dict[str, Any]) -> str:
    if source_type == "promotion":
        return _json_text(source.get("promotion_content"))
    benefits = source.get("benefits")
    if isinstance(benefits, list):
        parts = [_json_text(item) for item in benefits if _json_text(item)]
    else:
        parts = [_json_text(benefits)] if _json_text(benefits) else []
    commitment = source.get("minimum_commitment_months")
    if commitment is not None:
        parts.append(f"Minimum commitment months: {commitment}")
    return "\n".join(parts)


def _source_key(offer: dict[str, Any]) -> tuple[str, int] | None:
    promotion_id = offer.get("promotion_id")
    membership_plan_id = offer.get("membership_plan_id")
    if promotion_id is not None and membership_plan_id is None:
        return "promotion", int(promotion_id)
    if membership_plan_id is not None and promotion_id is None:
        return "membership", int(membership_plan_id)
    return None


def _base_row(
    offer: dict[str, Any],
    *,
    source_type: str | None,
    source: dict[str, Any] | None,
    status: str = "review",
    reason: str | None = None,
    conditions: list[str] | None = None,
) -> dict[str, Any]:
    source_id = None
    source_url = None
    source_updated_at = None
    if source:
        source_id = source.get("promotion_id") if source_type == "promotion" else source.get("plan_id")
        source_url = source.get("source_url")
        source_updated_at = source.get("updated_at")
    return {
        "offer_id": int(offer["id"]),
        "business_id": offer.get("business_id"),
        "source_type": source_type,
        "source_id": source_id,
        "source_url": source_url,
        "source_updated_at": source_updated_at,
        "offer_updated_at": offer.get("updated_at"),
        "offer_raw_text": str(offer.get("offer_raw_text") or ""),
        "conditions": conditions,
        "status": status,
        "reason": reason,
    }


def _resolve_group(
    offers: list[dict[str, Any]],
    *,
    source_type: str,
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    source_text = _source_text(source_type, source)
    if not source_text:
        return [
            _base_row(offer, source_type=source_type, source=source, reason="source_content_missing")
            for offer in offers
        ]
    output: list[dict[str, Any]] = []
    for offer in offers:
        conditions, reason = _extract_local_conditions(offer, source_text)
        if reason:
            output.append(_base_row(offer, source_type=source_type, source=source, reason=reason))
        else:
            output.append(
                _base_row(
                    offer,
                    source_type=source_type,
                    source=source,
                    status="ready",
                    conditions=conditions or [],
                )
            )
    return output


def build_plan(client: SupabaseRestClient) -> dict[str, Any]:
    offers = _fetch_all(
        client,
        "promo_offer_master",
        OFFER_SELECT,
        filters={"is_active": "eq.true", "conditions": "is.null"},
    )
    promotion_ids = {int(offer["promotion_id"]) for offer in offers if offer.get("promotion_id") is not None}
    membership_ids = {
        int(offer["membership_plan_id"])
        for offer in offers
        if offer.get("membership_plan_id") is not None
    }
    promotions = (
        {
            int(row["promotion_id"]): row
            for row in _fetch_all(
                client,
                "clinic_promotions",
                PROMOTION_SELECT,
                filters={"promotion_id": _in_filter(promotion_ids)},
            )
        }
        if promotion_ids
        else {}
    )
    memberships = (
        {
            int(row["plan_id"]): row
            for row in _fetch_all(
                client,
                "clinic_memberships",
                MEMBERSHIP_SELECT,
                filters={"plan_id": _in_filter(membership_ids)},
            )
        }
        if membership_ids
        else {}
    )
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    for offer in sorted(offers, key=lambda row: int(row["id"])):
        key = _source_key(offer)
        if key is None:
            rows.append(_base_row(offer, source_type=None, source=None, reason="no_condition_source"))
            continue
        source = promotions.get(key[1]) if key[0] == "promotion" else memberships.get(key[1])
        if source is None:
            source_stub = {"promotion_id": key[1]} if key[0] == "promotion" else {"plan_id": key[1]}
            rows.append(
                _base_row(
                    offer,
                    source_type=key[0],
                    source=source_stub,
                    reason="source_not_found",
                )
            )
            continue
        grouped[key].append(offer)

    for (source_type, source_id), source_offers in sorted(grouped.items()):
        source = promotions[source_id] if source_type == "promotion" else memberships[source_id]
        rows.extend(_resolve_group(source_offers, source_type=source_type, source=source))
    rows.sort(key=lambda row: int(row["offer_id"]))
    return {
        "plan_version": PLAN_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "active_conditions_null",
        "summary": {
            "targets": len(rows),
            "ready": sum(row["status"] == "ready" for row in rows),
            "review": sum(row["status"] == "review" for row in rows),
            "source_groups": len(grouped),
        },
        "rows": rows,
    }
