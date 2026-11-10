from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from utils.schema_contract import (
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_CLINIC_PROMOTIONS,
    TABLE_CLINIC_SERVICES,
    TABLE_FIRECRAWL_SCRAPE_RAW,
    TABLE_FIRECRAWL_SEARCH_RAW,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_access import fetch_all_rows


def build_actual_trace(
    *,
    domain: str,
    business_id: int,
    search_raw_id: int,
    scrape_rows: Sequence[Dict[str, Any]],
    membership_plan_ids: Sequence[int],
    service_ids: Sequence[int],
    offer_stats: Dict[str, Any],
) -> dict[str, Any]:
    return {
        "domain": domain,
        "business_id": business_id,
        "entry_table": "master_business_info",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "crawl": {
            "search_raw_table": TABLE_FIRECRAWL_SEARCH_RAW,
            "search_raw_id": search_raw_id,
            "scrape_raw_table": TABLE_FIRECRAWL_SCRAPE_RAW,
            "scrape_raw_ids": [int(r["id"]) for r in scrape_rows],
        },
        "extractions": [
            {
                "step": "1.1",
                "input": TABLE_FIRECRAWL_SEARCH_RAW,
                "input_id": search_raw_id,
                "schema": "membership_extraction_schema.json",
                "target_table": TABLE_CLINIC_MEMBERSHIPS,
                "target_ids": list(membership_plan_ids),
            },
            {
                "step": "1.2",
                "input": TABLE_FIRECRAWL_SEARCH_RAW,
                "input_id": search_raw_id,
                "schema": "clinic_services_firecrawl_extraction_schema.json",
                "target_table": TABLE_CLINIC_SERVICES,
                "target_ids": list(service_ids),
            },
            {
                "step": "2.2",
                "input": TABLE_FIRECRAWL_SCRAPE_RAW,
                "schema": "promotion_extraction_schema.json",
                "target_table": TABLE_CLINIC_PROMOTIONS,
                "count": offer_stats.get("promotions", 0),
            },
            {
                "step": "2.3",
                "input": TABLE_FIRECRAWL_SCRAPE_RAW,
                "schema": "offer_extraction_schema.json",
                "target_tables": [TABLE_PROMO_OFFER_MASTER, "promo_offer_items"],
                "offers": offer_stats.get("offers", 0),
                "items": offer_stats.get("items", 0),
            },
        ],
        "relations": [
            {
                "from_table": "promo_offer_items",
                "fk_column": "service_id",
                "to_table": TABLE_CLINIC_SERVICES,
            },
            {
                "from_table": TABLE_PROMO_OFFER_MASTER,
                "fk_column": "membership_plan_id",
                "to_table": TABLE_CLINIC_MEMBERSHIPS,
            },
        ],
        "forbidden": {
            "master_service_id_column": "service_id",
        },
    }


def resolve_business(client: Any, *, domain: str, business_id: Optional[int]) -> dict[str, Any]:
    if business_id is not None:
        rows = fetch_all_rows(
            client,
            "master_business_info",
            "business_id,name,website",
            filters={"business_id": f"eq.{business_id}"},
        )
        if rows:
            return rows[0]
    needle = domain.lower().strip()
    rows = fetch_all_rows(client, "master_business_info", "business_id,name,website", order="business_id.asc")
    for row in rows:
        site = str(row.get("website") or "").lower()
        if needle in site or site in needle:
            return row
    raise RuntimeError(f"No master_business_info row for domain={domain!r}")
