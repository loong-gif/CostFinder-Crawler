"""Canonical table/column names for the live Supabase schema."""
from __future__ import annotations

# Tables
TABLE_MASTER_BUSINESS = "master_business_info"
TABLE_CLINIC_MEMBERSHIPS = "clinic_memberships"
TABLE_CLINIC_SERVICES = "clinic_services"
TABLE_CLINIC_PROMOTIONS = "clinic_promotions"
TABLE_PROMO_OFFER_MASTER = "promo_offer_master"
TABLE_PROMO_OFFER_ITEMS = "promo_offer_items"
TABLE_FIRECRAWL_SEARCH_RAW = "firecrawl_search_raw"
TABLE_FIRECRAWL_SCRAPE_RAW = "firecrawl_scrape_raw"

# promo_offer_master columns (live DDL); conditions is nullable text[] and
# stores requirement strings only (source evidence is validated before write).
OFFER_MASTER_COLUMNS = (
    "id",
    "business_id",
    "membership_plan_id",
    "regular_price",
    "discount_price",
    "discount_percent",
    "discount_amount",
    "is_membership_required",
    "offer_fingerprint",
    "created_at",
    "offer_raw_text",
    "promotion_id",
    "is_active",
    "is_new_customer_required",
    "price_model",
    "updated_at",
    "conditions",
)

OFFER_MASTER_SELECT = ",".join(OFFER_MASTER_COLUMNS)

OFFER_MASTER_WITH_ITEMS_SELECT = (
    f"{OFFER_MASTER_SELECT},"
    "promo_offer_items(offer_item_id,offer_id,item_name,service_id,quantity:delivered_units,unit_price),"
    "clinic_promotions(promotion_id,source_url,promotion_title,campaign_start_date,campaign_end_date),"
    "clinic_memberships(plan_id,source_url,membership_name)"
)

OFFER_ITEM_COLUMNS = (
    "offer_item_id",
    "offer_id",
    "item_name",
    "service_id",
    "delivered_units",
    "created_at",
    "updated_at",
    "unit_price",
)

# Keep the internal extraction shape stable while reading the renamed database
# column. Writes must use the physical column name directly.
OFFER_ITEM_SELECT = (
    "offer_item_id,offer_id,item_name,service_id,quantity:delivered_units,"
    "created_at,updated_at,unit_price"
)

CLINIC_SERVICE_COLUMNS = (
    "service_id",
    "business_id",
    "service_name",
    "regular_price",
    "unit_type",
    "service_area",
    "created_at",
    "updated_at",
    "source_url",
    "service_category",
)

CLINIC_SERVICE_SELECT = ",".join(CLINIC_SERVICE_COLUMNS)

CLINIC_MEMBERSHIP_COLUMNS = (
    "plan_id",
    "business_id",
    "membership_name",
    "membership_price",
    "billing_period",
    "minimum_commitment_months",
    "created_at",
    "updated_at",
    "benefits",
    "source_url",
)

CLINIC_MEMBERSHIP_SELECT = ",".join(CLINIC_MEMBERSHIP_COLUMNS)

CLINIC_PROMOTION_COLUMNS = (
    "promotion_id",
    "business_id",
    "promotion_title",
    "source_url",
    "campaign_start_date",
    "campaign_end_date",
    "created_at",
    "updated_at",
    "is_active",
    "promotion_content",
)

CLINIC_PROMOTION_SELECT = ",".join(CLINIC_PROMOTION_COLUMNS)

def offer_is_active(row: dict) -> bool:
    """Return the current schema's activity flag."""
    return bool(row.get("is_active"))


def offer_item_name(row: dict, *, service_lookup: dict | None = None) -> str:
    """Primary display name from linked service, item embed, or legacy columns."""
    items = row.get("promo_offer_items")
    if isinstance(items, list) and items:
        for item in items:
            service_id = item.get("service_id")
            if service_lookup and service_id in service_lookup:
                name = str(service_lookup[service_id].get("service_name") or "").strip()
                if name:
                    return name
            name = str(item.get("service_name") or item.get("item_name") or "").strip()
            if name:
                return name
    if isinstance(items, dict):
        service_id = items.get("service_id")
        if service_lookup and service_id in service_lookup:
            name = str(service_lookup[service_id].get("service_name") or "").strip()
            if name:
                return name
        name = str(items.get("service_name") or items.get("item_name") or "").strip()
        if name:
            return name
    return str(row.get("service_name") or row.get("item_name") or "").strip()


def offer_source_url(row: dict) -> str:
    """Source URL from clinic_promotions embed or legacy master column."""
    promo = row.get("clinic_promotions")
    if isinstance(promo, dict):
        url = str(promo.get("source_url") or "").strip()
        if url:
            return url
    return str(row.get("source_url") or "").strip()


def offer_unit_type(row: dict) -> str:
    """Unit type from first item or legacy master column."""
    items = row.get("promo_offer_items")
    if isinstance(items, list) and items:
        unit = str(items[0].get("unit_type") or "").strip()
        if unit:
            return unit
    if isinstance(items, dict):
        unit = str(items.get("unit_type") or "").strip()
        if unit:
            return unit
    return str(row.get("unit_type") or "").strip()
