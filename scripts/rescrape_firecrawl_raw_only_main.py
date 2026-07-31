#!/usr/bin/env python3
"""Re-scrape promotion raw evidence through self-hosted Firecrawl v1 onlyMainContent."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.firecrawl_client import load_firecrawl_env
from utils.firecrawl_scrape_raw_db import canonical_scrape_url, scrape_response_to_row_fields
from utils.schema_contract import TABLE_FIRECRAWL_SCRAPE_RAW
from utils.supabase_access import fetch_all_rows, load_supabase_client

PROMOTION_PATH_RE = re.compile(r"/(?:special|promo)", re.IGNORECASE)
DEFAULT_FORMATS = ["markdown", "links"]
LEGACY_CONTENT_COLUMNS = ("html", "raw_html", "screenshot", "images")


def is_promotion_url(source_url: str) -> bool:
    return bool(PROMOTION_PATH_RE.search(urlparse(str(source_url or "")).path or ""))


def group_target_rows(rows: Iterable[dict[str, Any]]) -> dict[str, list[int]]:
    """Group promotion raw rows by canonical URL while retaining every target ID."""
    groups: dict[str, list[int]] = {}
    for row in rows:
        source_url = str(row.get("source_url") or "")
        canonical = canonical_scrape_url(source_url)
        if not canonical or not is_promotion_url(source_url):
            continue
        groups.setdefault(canonical, []).append(int(row["id"]))
    return groups


def build_v1_scrape_payload(source_url: str) -> dict[str, Any]:
    return {
        "url": source_url,
        "formats": list(DEFAULT_FORMATS),
        "onlyMainContent": True,
    }


def build_success_payload(response_json: dict[str, Any]) -> dict[str, Any]:
    """Build a row update that replaces evidence and clears stale full-page fields."""
    payload = scrape_response_to_row_fields(response_json)
    payload.update(
        {
            "success": True,
            "error_message": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    for column in LEGACY_CONTENT_COLUMNS:
        payload[column] = None
    return payload


def request_v1_scrape(
    api_url: str,
    api_key: str,
    source_url: str,
    *,
    timeout_secs: float,
) -> dict[str, Any]:
    response = requests.post(
        f"{api_url.rstrip('/')}/v1/scrape",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=build_v1_scrape_payload(source_url),
        timeout=timeout_secs,
    )
    try:
        body = response.json()
    except ValueError as exc:
        response.raise_for_status()
        raise RuntimeError("Firecrawl v1 scrape returned non-JSON") from exc
    if not response.ok or not body.get("success"):
        raise RuntimeError(str(body.get("error") or body.get("details") or f"HTTP {response.status_code}"))
    return body


def scrape_with_retries(
    api_url: str,
    api_key: str,
    source_url: str,
    *,
    retries: int,
    timeout_secs: float,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    last_error: Optional[str] = None
    for attempt in range(retries):
        try:
            return request_v1_scrape(api_url, api_key, source_url, timeout_secs=timeout_secs), None
        except Exception as exc:
            last_error = str(exc)
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    return None, last_error or "scrape failed"


def record_failure(client: Any, rows: Iterable[dict[str, Any]], error_message: str) -> int:
    """Record a retry failure only where no known good response would be downgraded."""
    updated = 0
    for row in rows:
        if row.get("success") and row.get("response_json"):
            continue
        client.update_row(
            TABLE_FIRECRAWL_SCRAPE_RAW,
            {"id": f"eq.{int(row['id'])}"},
            {
                "success": False,
                "error_message": error_message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        updated += 1
    return updated


def select_target_rows(
    client: Any,
    *,
    raw_ids: Iterable[int] = (),
    source_urls: Iterable[str] = (),
) -> list[dict[str, Any]]:
    rows = fetch_all_rows(
        client,
        TABLE_FIRECRAWL_SCRAPE_RAW,
        "id,source_url,success,response_json",
        order="id.asc",
    )
    selected_ids = {int(value) for value in raw_ids}
    selected_urls = {canonical_scrape_url(value) for value in source_urls if canonical_scrape_url(value)}
    if selected_ids:
        rows = [row for row in rows if int(row["id"]) in selected_ids]
    elif selected_urls:
        rows = [row for row in rows if canonical_scrape_url(str(row.get("source_url") or "")) in selected_urls]
    return [row for row in rows if is_promotion_url(str(row.get("source_url") or ""))]


def run_rescrape(
    client: Any,
    *,
    api_url: str,
    api_key: str,
    apply: bool,
    limit: Optional[int],
    raw_ids: Iterable[int] = (),
    source_urls: Iterable[str] = (),
    retries: int = 3,
    timeout_secs: float = 150,
) -> dict[str, Any]:
    rows = select_target_rows(client, raw_ids=raw_ids, source_urls=source_urls)
    grouped_ids = group_target_rows(rows)
    row_by_id = {int(row["id"]): row for row in rows}
    urls = sorted(grouped_ids)
    if limit is not None:
        urls = urls[:limit]

    summary: dict[str, Any] = {
        "apply": apply,
        "selected_rows": sum(len(grouped_ids[url]) for url in urls),
        "unique_urls": len(urls),
        "updated_rows": 0,
        "successful_urls": 0,
        "failed_urls": 0,
        "failure_rows_recorded": 0,
        "errors": [],
    }
    if not apply:
        return summary

    for source_url in urls:
        response_json, error_message = scrape_with_retries(
            api_url,
            api_key,
            source_url,
            retries=retries,
            timeout_secs=timeout_secs,
        )
        row_ids = grouped_ids[source_url]
        target_rows = [row_by_id[row_id] for row_id in row_ids]
        if response_json is None:
            summary["failed_urls"] += 1
            summary["failure_rows_recorded"] += record_failure(client, target_rows, error_message or "scrape failed")
            summary["errors"].append({"source_url": source_url, "error": error_message})
            continue

        payload = build_success_payload(response_json)
        for row_id in row_ids:
            client.update_row(TABLE_FIRECRAWL_SCRAPE_RAW, {"id": f"eq.{row_id}"}, payload)
        summary["successful_urls"] += 1
        summary["updated_rows"] += len(row_ids)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write successful re-scrapes to Supabase.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum unique canonical URLs to process.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--raw-id", action="append", type=int, default=[])
    selection.add_argument("--source-url", action="append", default=[])
    parser.add_argument("--firecrawl-api-url", default=os.getenv("FIRECRAWL_SELF_HOSTED_API_URL", ""))
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout-secs", type=float, default=150)
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON report path.")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.retries <= 0 or args.timeout_secs <= 0:
        parser.error("--retries and --timeout-secs must be positive")
    if args.apply and not args.firecrawl_api_url:
        parser.error("--firecrawl-api-url or FIRECRAWL_SELF_HOSTED_API_URL is required with --apply")

    client = load_supabase_client()
    api_key = ""
    if args.apply:
        load_firecrawl_env()
        api_key = str(os.getenv("FIRECRAWL_API_KEY") or "").strip()
    summary = run_rescrape(
        client,
        api_url=args.firecrawl_api_url,
        api_key=api_key,
        apply=args.apply,
        limit=args.limit,
        raw_ids=args.raw_id,
        source_urls=args.source_url,
        retries=args.retries,
        timeout_secs=args.timeout_secs,
    )
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if summary["failed_urls"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
