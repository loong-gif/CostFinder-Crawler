#!/usr/bin/env python3
"""
Run the scheduled cleanup check for promo_social_staging.
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+.*")

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_DIR

TABLE_NAME = "promo_social_staging"
TIMESTAMP_CANDIDATES = [
    "timestamp",
    "crawl_timestamp",
    "created_at",
    "updated_at",
    "posted_at",
    "published_at",
    "inserted_at",
]
DEFAULT_TIMEZONE = os.getenv("TZ") or "Asia/Shanghai"


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"select": select}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = str(limit)
        if order:
            params["order"] = order

        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def count_rows(self, table: str, *, filters: Optional[Dict[str, str]] = None) -> int:
        params: Dict[str, str] = {"select": "*"}
        if filters:
            params.update(filters)

        response = self.session.head(
            f"{self.base_url}/{table}",
            params=params,
            headers={"Prefer": "count=exact"},
            timeout=30,
        )
        response.raise_for_status()
        count = parse_content_range_total(response.headers.get("Content-Range"))
        if count is None:
            raise RuntimeError("Supabase 未返回可解析的总行数")
        return count

    def delete_rows(self, table: str, *, filters: Dict[str, str]) -> Optional[int]:
        response = self.session.delete(
            f"{self.base_url}/{table}",
            params=filters,
            headers={"Prefer": "count=exact"},
            timeout=30,
        )
        response.raise_for_status()
        return parse_content_range_total(response.headers.get("Content-Range"))


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_content_range_total(content_range: Optional[str]) -> Optional[int]:
    if not content_range:
        return None

    total = content_range.rsplit("/", 1)[-1].strip()
    if total == "*":
        return None
    return int(total)


def current_local_time() -> datetime:
    return datetime.now(ZoneInfo(DEFAULT_TIMEZONE))


def resolve_report_path(now: datetime) -> Path:
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"promo_social_staging_cleanup_{timestamp}.json"


def detect_timestamp_column(client: SupabaseRestClient, cutoff_iso: str) -> str:
    sample_rows = client.fetch_rows(TABLE_NAME, "*", limit=1)
    if sample_rows:
        available = {key for key in sample_rows[0].keys() if key in TIMESTAMP_CANDIDATES}
        for candidate in TIMESTAMP_CANDIDATES:
            if candidate in available:
                return candidate

    for candidate in TIMESTAMP_CANDIDATES:
        try:
            client.count_rows(TABLE_NAME, filters={candidate: f"lt.{cutoff_iso}"})
            return candidate
        except requests.HTTPError as exc:
            response = exc.response
            if response is not None and response.status_code == 400:
                continue
            raise

    raise RuntimeError("未能识别 promo_social_staging 的时间字段")


def build_skip_report(now: datetime, cutoff: datetime) -> Dict[str, Any]:
    return {
        "status": "skipped",
        "table": TABLE_NAME,
        "local_timezone": DEFAULT_TIMEZONE,
        "current_local_timestamp": now.isoformat(),
        "is_first_day_of_month": False,
        "cleanup_executed": False,
        "matched_rows": 0,
        "deleted_rows": 0,
        "cutoff_timestamp": cutoff.isoformat(),
        "skip_reason": "local_date_is_not_first_day_of_month",
    }


def build_success_report(
    now: datetime,
    cutoff: datetime,
    *,
    timestamp_column: str,
    matched_rows: int,
    deleted_rows: int,
    remaining_rows_after_delete: int,
    delete_count_reported_by_api: Optional[int],
) -> Dict[str, Any]:
    return {
        "status": "completed",
        "table": TABLE_NAME,
        "local_timezone": DEFAULT_TIMEZONE,
        "current_local_timestamp": now.isoformat(),
        "is_first_day_of_month": True,
        "cleanup_executed": True,
        "timestamp_column": timestamp_column,
        "matched_rows": matched_rows,
        "deleted_rows": deleted_rows,
        "remaining_rows_after_delete": remaining_rows_after_delete,
        "cutoff_timestamp": cutoff.isoformat(),
        "delete_count_reported_by_api": delete_count_reported_by_api,
    }


def build_error_report(now: datetime, cutoff: datetime, exc: Exception) -> Dict[str, Any]:
    return {
        "status": "error",
        "table": TABLE_NAME,
        "local_timezone": DEFAULT_TIMEZONE,
        "current_local_timestamp": now.isoformat(),
        "is_first_day_of_month": now.day == 1,
        "cleanup_executed": False,
        "matched_rows": 0,
        "deleted_rows": 0,
        "cutoff_timestamp": cutoff.isoformat(),
        "error": str(exc),
    }


def write_report(report_path: Path, report: Dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    now = current_local_time()
    cutoff = now - timedelta(days=30)
    report_path = resolve_report_path(now)

    try:
        if now.day != 1:
            report = build_skip_report(now, cutoff)
        else:
            client = load_supabase_client()
            cutoff_iso = cutoff.isoformat()
            timestamp_column = detect_timestamp_column(client, cutoff_iso)
            filters = {timestamp_column: f"lt.{cutoff_iso}"}
            matched_rows = client.count_rows(TABLE_NAME, filters=filters)

            delete_count_reported_by_api: Optional[int] = 0
            if matched_rows > 0:
                delete_count_reported_by_api = client.delete_rows(TABLE_NAME, filters=filters)

            remaining_rows_after_delete = client.count_rows(TABLE_NAME, filters=filters)
            deleted_rows = matched_rows - remaining_rows_after_delete
            report = build_success_report(
                now,
                cutoff,
                timestamp_column=timestamp_column,
                matched_rows=matched_rows,
                deleted_rows=deleted_rows,
                remaining_rows_after_delete=remaining_rows_after_delete,
                delete_count_reported_by_api=delete_count_reported_by_api,
            )
    except Exception as exc:
        report = build_error_report(now, cutoff, exc)

    write_report(report_path, report)
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "status": report["status"],
                "matched_rows": report["matched_rows"],
                "deleted_rows": report["deleted_rows"],
                "cutoff_timestamp": report["cutoff_timestamp"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
