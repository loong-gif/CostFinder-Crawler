"""Supabase helpers for promo_offer_items."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional

from utils.schema_contract import TABLE_PROMO_OFFER_ITEMS, OFFER_ITEM_SELECT
from utils.supabase_rest import SupabaseRestClient


_BLANKET_OFFER_RE = re.compile(
    r"\b(?:all services|any service|all products|all treatments|gift cards?|"
    r"skin ?care products|products and services|products & services|entire menu|"
    r"all regular-priced services|all medspa services|all purchases|referral program(?:me)?|"
    r"(?:favorite|selected|select) treatments?(?: and products?)?)\b",
    re.IGNORECASE,
)


# The AGENTS.md batch check, verbatim; usable as a Python regex and inside SQL `~*`.
AGENTS_BLANKET_PATTERN = r"all services|any service|all products|all treatments|gift card|skin ?care products"
AGENTS_BLANKET_RE = re.compile(AGENTS_BLANKET_PATTERN, re.IGNORECASE)


def is_blanket_offer_text(text: str) -> bool:
    """Identify explicit clinic-wide and non-treatment offer wording."""
    text = str(text or "")
    return bool(_BLANKET_OFFER_RE.search(text) or AGENTS_BLANKET_RE.search(text))


def offer_item_semantics(items: List[Dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Compare persisted and extraction item shapes without timestamps or IDs."""
    return sorted([
        (item.get("service_id"), item.get("item_name"),
         item.get("quantity", item.get("delivered_units")), item.get("unit_price"))
        for item in items
    ], key=repr)


def _service_name_for_id(client: SupabaseRestClient, service_id: int) -> str:
    services = client.fetch_rows(
        "clinic_services",
        "service_id,service_name",
        filters={"service_id": f"eq.{service_id}"},
        limit=1,
    )
    if len(services) != 1:
        raise ValueError(f"service_id {service_id} does not resolve to one clinic service")
    return str(services[0]["service_name"])


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
    """Reconcile an Offer's items while preserving uniquely matched item IDs."""
    if not items:
        return []
    existing = [dict(row) for row in fetch_items_for_offer(client, offer_id)]
    now = datetime.now(timezone.utc).isoformat()
    rows: List[Dict[str, Any]] = []
    for item in items:
        service_id = item.get("service_id")
        row: Dict[str, Any] = {
            "offer_id": offer_id,
            "created_at": now,
            "updated_at": now,
        }
        row["service_id"] = service_id
        if item.get("item_name") is not None:
            row["item_name"] = str(item["item_name"]).strip()
        elif item.get("service_name") is not None:
            row["item_name"] = str(item["service_name"]).strip()
        elif service_id is not None:
            row["item_name"] = _service_name_for_id(client, int(service_id))
        row["delivered_units"] = item.get("quantity", item.get("delivered_units"))
        row["unit_price"] = item.get("unit_price")
        rows.append(row)
    if not rows:
        return []
    if offer_item_semantics(existing) == offer_item_semantics(rows):
        return existing
    assignments, used = [], set()
    for row in rows:
        matches = [old for old in existing if old.get("offer_item_id") not in used
                   and old.get("service_id") == row.get("service_id")
                   and old.get("item_name") == row.get("item_name")]
        if not matches and row.get("service_id") is not None:
            matches = [old for old in existing if old.get("offer_item_id") not in used
                       and old.get("service_id") == row.get("service_id")]
        if len(matches) > 1:
            raise ValueError("ambiguous_offer_item_identity")
        old = matches[0] if matches else None
        if old:
            used.add(old["offer_item_id"])
        assignments.append((row, old))
    result = []
    for row, old in assignments:
        if old is None:
            result.extend(client.insert_rows(TABLE_PROMO_OFFER_ITEMS, [row]))
            continue
        fields = ("service_id", "item_name", "delivered_units", "unit_price")
        patch = {
            key: row.get(key) for key in fields
            if row.get(key) != (old.get("quantity", old.get("delivered_units")) if key == "delivered_units" else old.get(key))}
        if not patch:
            result.append(old)
            continue
        filters = {"offer_item_id": f"eq.{old['offer_item_id']}"}
        if old.get("updated_at"):
            filters["updated_at"] = f"eq.{old['updated_at']}"
        updated = client.update_row(TABLE_PROMO_OFFER_ITEMS, filters, {**patch, "updated_at": now})
        if len(updated) != 1:
            raise ValueError("offer_item_write_conflict")
        result.extend(updated)
    for old in existing:
        if old.get("offer_item_id") not in used:
            filters = {"offer_item_id": f"eq.{old['offer_item_id']}"}
            if old.get("updated_at"):
                filters["updated_at"] = f"eq.{old['updated_at']}"
            deleted = client.delete_rows(TABLE_PROMO_OFFER_ITEMS, filters)
            if len(deleted) != 1:
                raise ValueError("offer_item_write_conflict")
    return result


def link_item_to_service(
    client: SupabaseRestClient,
    offer_item_id: int,
    service_id: int,
) -> List[Dict[str, Any]]:
    existing = client.fetch_rows(
        TABLE_PROMO_OFFER_ITEMS,
        "offer_item_id,item_name",
        filters={"offer_item_id": f"eq.{offer_item_id}"},
        limit=1,
    )
    if len(existing) != 1:
        raise ValueError(f"offer_item_id {offer_item_id} does not resolve to one promo offer item")
    item_name = str(existing[0].get("item_name") or "").strip()
    payload: Dict[str, Any] = {
        "service_id": service_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if not item_name:
        payload["item_name"] = _service_name_for_id(client, service_id)
    return client.update_row(
        TABLE_PROMO_OFFER_ITEMS,
        {"offer_item_id": f"eq.{offer_item_id}"},
        payload,
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
        if is_blanket_offer_text(hint):
            continue
        items = fetch_items_for_offer(client, offer_id)
        unlinked = [item for item in items if item.get("service_id") is None]
        if not unlinked:
            continue
        sibling_count = len(items)
        is_package = len(items) > 1  # ponytail: backfill lacks master.is_package; multi-item uses isolated mapping
        for item in unlinked:
            name = infer_service_name_for_item(
                offer_raw_text=hint,
                quantity=item.get("quantity"),
                service_name=item.get("service_name"),
                item_name=item.get("item_name"),
                sibling_count=sibling_count,
                is_package=is_package,
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
        offer.get("item_name")
        or offer.get("offer_raw_text")
        or offer.get("service_name")
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
