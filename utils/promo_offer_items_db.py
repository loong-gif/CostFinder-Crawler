"""Supabase helpers for promo_offer_items."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.schema_contract import TABLE_PROMO_OFFER_ITEMS, OFFER_ITEM_SELECT
from utils.supabase_rest import SupabaseRestClient


def fetch_items_for_offer(
    client: SupabaseRestClient,
    offer_id: int,
) -> List[Dict[str, Any]]:
    return client.fetch_rows(
        TABLE_PROMO_OFFER_ITEMS,
        OFFER_ITEM_SELECT,
        filters={"offer_id": f"eq.{offer_id}"},
        order="offer_item_id.asc",
    )


def upsert_offer_items(
    client: SupabaseRestClient,
    offer_id: int,
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Replace items for an offer with the provided list."""
    if not items:
        return []
    existing = fetch_items_for_offer(client, offer_id)
    if existing:
        client.delete_rows(TABLE_PROMO_OFFER_ITEMS, {"offer_id": f"eq.{offer_id}"})
    now = datetime.now(timezone.utc).isoformat()
    rows: List[Dict[str, Any]] = []
    for item in items:
        row: Dict[str, Any] = {
            "offer_id": offer_id,
            "created_at": now,
            "updated_at": now,
        }
        if item.get("service_id") is not None:
            row["service_id"] = item["service_id"]
        if item.get("quantity") is not None:
            row["quantity"] = item["quantity"]
        if item.get("unit_price") is not None:
            row["unit_price"] = item["unit_price"]
        rows.append(row)
    if not rows:
        rows = [{"offer_id": offer_id, "created_at": now, "updated_at": now}]
    return client.insert_rows(TABLE_PROMO_OFFER_ITEMS, rows)


def link_item_to_service(
    client: SupabaseRestClient,
    offer_item_id: int,
    service_id: int,
) -> List[Dict[str, Any]]:
    return client.update_row(
        TABLE_PROMO_OFFER_ITEMS,
        {"offer_item_id": f"eq.{offer_item_id}"},
        {
            "service_id": service_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def backfill_unlinked_item_service_ids(
    client: SupabaseRestClient,
    *,
    business_ids: List[int] | None = None,
) -> Dict[str, Any]:
    """Link promo_offer_items.service_id from offer_raw_text + clinic_services."""
    from utils.clinic_service_extraction import (
        infer_service_name_for_item,
        resolve_service_row_for_name,
    )
    from utils.schema_contract import TABLE_PROMO_OFFER_MASTER

    filters: Dict[str, str] = {"is_active": "eq.true"}
    if business_ids:
        filters["business_id"] = f"in.({','.join(str(i) for i in business_ids)})"
    offers = client.fetch_rows(
        TABLE_PROMO_OFFER_MASTER,
        "id,business_id,offer_raw_text",
        filters=filters,
        limit=2000,
    )
    linked = 0
    skipped: List[Dict[str, Any]] = []
    for offer in offers:
        offer_id = int(offer["id"])
        business_id = int(offer["business_id"])
        hint = str(offer.get("offer_raw_text") or "")
        items = fetch_items_for_offer(client, offer_id)
        unlinked = [item for item in items if item.get("service_id") is None]
        if not unlinked:
            continue
        for item in unlinked:
            name = infer_service_name_for_item(
                offer_raw_text=hint,
                quantity=item.get("quantity"),
                sibling_count=len(unlinked),
            )
            svc = resolve_service_row_for_name(
                client, business_id=business_id, service_name=name
            )
            if not svc:
                skipped.append(
                    {
                        "offer_item_id": item.get("offer_item_id"),
                        "business_id": business_id,
                        "inferred": name,
                        "offer_raw_text": hint[:120],
                    }
                )
                continue
            link_item_to_service(client, int(item["offer_item_id"]), int(svc["service_id"]))
            linked += 1
    return {"linked": linked, "skipped": skipped}


def build_item_from_offer_fields(
    offer: Dict[str, Any],
    *,
    service_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a single item row payload from extracted offer dict."""
    name = str(
        offer.get("service_name")
        or offer.get("item_name")
        or offer.get("offer_raw_text")
        or "Offer item"
    ).strip()
    item: Dict[str, Any] = {"item_name": name[:500] if len(name) > 500 else name}
    if offer.get("unit_type"):
        item["unit_type"] = str(offer["unit_type"]).strip()
    if offer.get("quantity") is not None:
        item["quantity"] = offer["quantity"]
    if service_id is not None:
        item["service_id"] = service_id
    return item
