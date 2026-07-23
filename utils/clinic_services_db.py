"""Supabase read/write helpers for clinic_services."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from utils.clinic_services_botox import BotoxServiceFields
from utils.clinic_services_from_offers import ClinicServiceFromOfferFields
from utils.schema_contract import CLINIC_SERVICE_SELECT, TABLE_CLINIC_SERVICES
from utils.supabase_rest import SupabaseRestClient

TABLE = TABLE_CLINIC_SERVICES


def fetch_service_row(
    client: SupabaseRestClient,
    business_id: int,
    service_name: str,
) -> Optional[Dict[str, Any]]:
    rows = client.fetch_rows(
        TABLE,
        CLINIC_SERVICE_SELECT,
        filters={
            "business_id": f"eq.{business_id}",
            "service_name": f"eq.{service_name}",
        },
        limit=1,
    )
    return rows[0] if rows else None


def fetch_rows_for_refresh(
    client: SupabaseRestClient,
    *,
    service_name: str,
    older_than_days: Optional[int] = None,
    business_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    filters: Dict[str, str] = {"service_name": f"eq.{service_name}"}
    if business_id is not None:
        filters["business_id"] = f"eq.{business_id}"
    rows = client.fetch_rows(
        TABLE,
        CLINIC_SERVICE_SELECT,
        filters=filters,
        limit=5000,
        order="business_id.asc",
    )
    if older_than_days is None:
        return rows
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    out: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("regular_price") is None:
            out.append(row)
            continue
        updated_raw = row.get("updated_at")
        if not updated_raw:
            out.append(row)
            continue
        try:
            updated_at = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
        except ValueError:
            out.append(row)
            continue
        if updated_at < cutoff:
            out.append(row)
    return out


def seed_skeleton(
    client: SupabaseRestClient,
    business_id: int,
    service_name: str,
) -> Dict[str, Any]:
    existing = fetch_service_row(client, business_id, service_name)
    if existing:
        return existing
    inserted = client.insert_rows(
        TABLE,
        [{"business_id": business_id, "service_name": service_name}],
    )
    return inserted[0]


def _decimal_to_api(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def apply_fields(
    client: SupabaseRestClient,
    service_id: int,
    fields: BotoxServiceFields | ClinicServiceFromOfferFields,
    *,
    force_price: bool = False,
    existing_price: Optional[Any] = None,
    existing_row: Optional[Dict[str, Any]] = None,
    source_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """PATCH clinic_services; metadata can update without regular_price."""
    from utils.service_price_guard import (
        is_catalog_ineligible_url,
        normalize_source_url,
        should_replace_source_url,
    )

    payload: Dict[str, Any] = {}
    ineligible_source = bool(source_url and is_catalog_ineligible_url(source_url))
    if fields.regular_price is not None and not ineligible_source:
        if force_price or existing_price is None:
            payload["regular_price"] = _decimal_to_api(fields.regular_price)
    if fields.unit_type and not (existing_row or {}).get("unit_type"):
        payload["unit_type"] = fields.unit_type
    elif fields.unit_type:
        payload["unit_type"] = fields.unit_type
    if fields.service_area and not (existing_row or {}).get("service_area"):
        payload["service_area"] = fields.service_area
    elif fields.service_area:
        payload["service_area"] = fields.service_area
    if source_url and not ineligible_source:
        incoming = normalize_source_url(source_url)
        existing_url = normalize_source_url((existing_row or {}).get("source_url"))
        if should_replace_source_url(existing_url, incoming):
            payload["source_url"] = incoming

    if not payload:
        return []

    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    return client.update_row(
        TABLE,
        {"service_id": f"eq.{service_id}"},
        payload,
    )
