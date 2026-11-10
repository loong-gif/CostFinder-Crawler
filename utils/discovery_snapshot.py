"""discover-pages 与 review-pages 的只读数据库快照：pipeline_db.fresh_snapshot 生成并执行 read.sql（Supabase CLI --linked）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.discovery_batch import snapshot_dir
from utils.firecrawl_scrape_raw_db import canonical_scrape_url
from utils.pipeline_db import fresh_snapshot, load_snapshot
from utils.promo_value_review import BusinessContext

DISCOVERY_TABLES = {
    "businesses": ("master_business_info", "business_id, name, city, review_count, score, website", "true"),
    "promotions": ("clinic_promotions", "promotion_id, business_id, is_active, source_url", "true"),
    "memberships": ("clinic_memberships", "plan_id, business_id, source_url", "true"),
    "services": ("clinic_services", "service_id, business_id, source_url", "true"),
    "offers": ("promo_offer_master", "id, business_id", "true"),
}


def read_discovery_snapshot(batch: Path) -> dict[str, list[dict[str, Any]]]:
    return load_snapshot(fresh_snapshot(snapshot_dir(batch), DISCOVERY_TABLES), DISCOVERY_TABLES)


def business_rows(snapshot: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """有网站的商家，按 business_id 排序。"""
    rows = [row for row in snapshot["businesses"] if str(row.get("website") or "").strip()]
    return sorted(rows, key=lambda row: row["business_id"])


def known_urls(snapshot: dict[str, list[dict[str, Any]]]) -> set[str]:
    """已经作为促销、会员或服务来源入库的页面，不再作为新页面发现。"""
    known = {canonical_scrape_url(row["source_url"]) for table in ("promotions", "memberships", "services")
             for row in snapshot[table] if row.get("source_url")}
    known.discard("")
    return known


def business_contexts(snapshot: dict[str, list[dict[str, Any]]]) -> dict[int, BusinessContext]:
    touched: set[int] = set()
    active: set[int] = set()
    for row in snapshot["promotions"]:
        if row.get("business_id") is not None:
            touched.add(row["business_id"])
            if row.get("is_active"):
                active.add(row["business_id"])
    for table in ("memberships", "services", "offers"):
        for row in snapshot[table]:
            if row.get("business_id") is not None:
                touched.add(row["business_id"])
                if table == "memberships":
                    active.add(row["business_id"])
    return {
        row["business_id"]: BusinessContext(
            business_id=row["business_id"], name=row.get("name") or "", city=row.get("city") or "",
            review_count=row.get("review_count"), score=row.get("score"), website=row.get("website") or "",
            has_active_offer=row["business_id"] in active, never_processed=row["business_id"] not in touched)
        for row in snapshot["businesses"]
    }
