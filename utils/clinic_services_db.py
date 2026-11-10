"""Supabase read/write helpers for clinic_services."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from utils.clinic_services_botox import BotoxServiceFields
from utils.clinic_services_from_offers import ClinicServiceFromOfferFields
from utils.clinic_service_taxonomy import service_name_enum_hint
from utils.schema_contract import CLINIC_SERVICE_SELECT, TABLE_CLINIC_SERVICES
from utils.supabase_rest import SupabaseRestClient

TABLE = TABLE_CLINIC_SERVICES

def fetch_service_row(
    client: SupabaseRestClient,
    business_id: int,
    service_name: str,
) -> Optional[Dict[str, Any]]:
    canonical_name = service_name_enum_hint(service_name)
    if canonical_name is None:
        return None
    rows = client.fetch_rows(
        TABLE,
        CLINIC_SERVICE_SELECT,
        filters={
            "business_id": f"eq.{business_id}",
            "service_name": f"eq.{canonical_name}",
        },
        limit=1,
    )
    return rows[0] if rows else None


def fetch_service_row_by_enum(
    client: SupabaseRestClient,
    business_id: int,
    service_name_enum: str,
) -> Optional[Dict[str, Any]]:
    """Find a service by canonical service_name enum."""
    rows = client.fetch_rows(
        TABLE,
        CLINIC_SERVICE_SELECT,
        filters={
            "business_id": f"eq.{business_id}",
            "service_name": f"eq.{service_name_enum}",
        },
        limit=1,
    )
    return rows[0] if rows else None


def seed_skeleton(
    client: SupabaseRestClient,
    business_id: int,
    service_name: str,
) -> Dict[str, Any]:
    canonical_name = service_name_enum_hint(service_name)
    if canonical_name is None:
        raise ValueError("unsupported clinic service enum")
    existing = fetch_service_row(client, business_id, canonical_name)
    if existing:
        return existing
    inserted = client.insert_rows(
        TABLE,
        [
            {
                "business_id": business_id,
                "service_name": canonical_name,
            }
        ],
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
