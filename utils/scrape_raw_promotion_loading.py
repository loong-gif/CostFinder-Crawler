from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from utils.promo_paths import is_promotion_url_loose as is_promotion_page_url
from utils.firecrawl_scrape_raw_db import canonical_scrape_url
from utils.recent_raw_extraction import promotion_evidence_markdown

PAGE_SIZE = 1000
SCRAPE_SELECT = (
    "id,source_url,success,created_at,updated_at,markdown,markdown_ocr,metadata"
)


def batch_utc_bounds(batch_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    """Return the requested local calendar day as a half-open UTC interval."""
    requested_day = date.fromisoformat(str(batch_date).strip())
    requested_zone = ZoneInfo(str(timezone_name).strip())
    local_start = datetime.combine(requested_day, datetime_time.min, tzinfo=requested_zone)
    local_end = datetime.combine(
        requested_day + timedelta(days=1),
        datetime_time.min,
        tzinfo=requested_zone,
    )
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fetch_scrape_window(
    client: Any,
    *,
    timestamp_column: str,
    start: datetime,
    end: datetime,
    raw_ids: Optional[Sequence[int]],
) -> list[dict[str, Any]]:
    filters = {
        "success": "eq.true",
        timestamp_column: f"gte.{start.isoformat()}",
        # PostgREST permits applying multiple operators to one column as a comma
        # expression.  Passing ``and`` would make the test double diverge from the
        # production client, so the upper bound is expressed as a top-level and.
        "and": f"({timestamp_column}.lt.{end.isoformat()})",
    }
    if raw_ids:
        filters["id"] = f"in.({','.join(str(int(raw_id)) for raw_id in raw_ids)})"

    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(
            "firecrawl_scrape_raw",
            SCRAPE_SELECT,
            filters=dict(filters),
            limit=PAGE_SIZE,
            offset=offset,
            order="id.asc",
        )
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def load_batch_scrapes(
    client: Any,
    *,
    batch_date: str,
    timezone_name: str,
    raw_ids: Optional[Sequence[int]] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Union created/updated batch windows and keep the newest row per page URL."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    start, end = batch_utc_bounds(batch_date, timezone_name)
    rows = _fetch_scrape_window(
        client,
        timestamp_column="created_at",
        start=start,
        end=end,
        raw_ids=raw_ids,
    )
    rows.extend(
        _fetch_scrape_window(
            client,
            timestamp_column="updated_at",
            start=start,
            end=end,
            raw_ids=raw_ids,
        )
    )

    newest_by_url: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("success") is not True:
            continue
        source_url = str(row.get("source_url") or "").strip()
        canonical_url = canonical_scrape_url(source_url)
        if not canonical_url or not is_promotion_page_url(canonical_url):
            continue
        if not promotion_evidence_markdown(row).strip():
            continue
        current = newest_by_url.get(canonical_url)
        freshness = (_parse_timestamp(row.get("updated_at")), int(row.get("id") or 0))
        current_freshness = (
            _parse_timestamp((current or {}).get("updated_at")),
            int((current or {}).get("id") or 0),
        )
        if current is None or freshness > current_freshness:
            newest_by_url[canonical_url] = {**row, "source_url": canonical_url}

    selected = sorted(
        newest_by_url.values(),
        key=lambda row: (_parse_timestamp(row.get("updated_at")), int(row.get("id") or 0)),
    )
    return selected if limit is None else selected[:limit]


def _load_all_businesses(client: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(
            "master_business_info",
            "business_id,name,website,address,city",
            limit=PAGE_SIZE,
            offset=offset,
            order="business_id.asc",
        )
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def _metadata_text(row: dict[str, Any], field_name: str) -> str:
    metadata = row.get("metadata")
    return str(metadata.get(field_name) or "") if isinstance(metadata, dict) else ""
