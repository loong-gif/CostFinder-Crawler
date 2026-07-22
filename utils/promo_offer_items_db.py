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
