"""Supabase helpers for clinic_promotions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from utils.schema_contract import TABLE_CLINIC_PROMOTIONS, CLINIC_PROMOTION_SELECT
from utils.supabase_rest import SupabaseRestClient


def _norm_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _title_from_url(source_url: str) -> str:
    path = (urlparse(source_url).path or "/").strip("/")
    if not path:
        return source_url
    segment = path.split("/")[-1]
    return segment.replace("-", " ").replace("_", " ").title() or source_url


def fetch_promotion_by_url(
    client: SupabaseRestClient,
    source_url: str,
    *,
    business_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    norm = _norm_url(source_url)
    if not norm:
        return None
    filters: Dict[str, str] = {"source_url": f"eq.{norm}"}
    if business_id is not None:
        filters["business_id"] = f"eq.{business_id}"
    rows = client.fetch_rows(TABLE_CLINIC_PROMOTIONS, CLINIC_PROMOTION_SELECT, filters=filters, limit=1)
    return rows[0] if rows else None


def upsert_promotion(
    client: SupabaseRestClient,
    *,
    business_id: int,
    source_url: str,
    promotion_title: Optional[str] = None,
    needs_ocr: bool = False,
) -> int:
    del needs_ocr  # live clinic_promotions has no needs_ocr column
    norm = _norm_url(source_url)
    if not norm:
        raise ValueError("source_url is required")
    existing = fetch_promotion_by_url(client, norm, business_id=business_id)
    if existing:
        return int(existing["promotion_id"])
    title = (promotion_title or _title_from_url(norm)).strip() or norm
    now = datetime.now(timezone.utc).isoformat()
    inserted = client.insert_rows(
        TABLE_CLINIC_PROMOTIONS,
        [
            {
                "business_id": business_id,
                "source_url": norm,
                "promotion_title": title,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    return int(inserted[0]["promotion_id"])
