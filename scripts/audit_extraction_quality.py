#!/usr/bin/env python3
"""Unified read-only audit for clinic extraction tables + raw lineage."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING  # noqa: E402
from utils.extraction_quality_audit import AuditReport, run_full_audit  # noqa: E402
from utils.schema_contract import (  # noqa: E402
    CLINIC_MEMBERSHIP_SELECT,
    CLINIC_PROMOTION_SELECT,
    CLINIC_SERVICE_SELECT,
    OFFER_ITEM_SELECT,
    OFFER_MASTER_WITH_ITEMS_SELECT,
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_CLINIC_PROMOTIONS,
    TABLE_CLINIC_SERVICES,
    TABLE_FIRECRAWL_SCRAPE_RAW,
    TABLE_FIRECRAWL_SEARCH_RAW,
    TABLE_PROMO_OFFER_ITEMS,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402

DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"
DEFAULT_RESULT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 1000
TABLE_ORDER = {
    TABLE_CLINIC_SERVICES: "service_id.asc",
    TABLE_CLINIC_MEMBERSHIPS: "plan_id.asc",
    TABLE_CLINIC_PROMOTIONS: "promotion_id.asc",
    TABLE_PROMO_OFFER_MASTER: "id.asc",
    TABLE_PROMO_OFFER_ITEMS: "offer_item_id.asc",
    "master_business_info": "business_id.asc",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit clinic extraction quality across five tables")
    parser.add_argument("--limit", type=int, default=None, help="只拉取前 N 条记录用于快速检查")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--result-dir", default=str(DEFAULT_RESULT_DIR))
    parser.add_argument("--canvas-data", default="", help="输出 Canvas 内嵌 JSON 路径")
    return parser.parse_args()


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = get_supabase_writer_key()
    if not base_url or not key:
        raise RuntimeError("缺少 SUPABASE_URL 或 writer key")
    return SupabaseRestClient(base_url, key)


def fetch_all(
    client: SupabaseRestClient,
    table: str,
    select: str,
    *,
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    remaining = limit
    while True:
        page_limit = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
        batch = client.fetch_rows(
            table,
            select,
            limit=page_limit,
            offset=offset,
            order=TABLE_ORDER.get(table, "id.asc"),
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit
        if remaining is not None:
            remaining -= len(batch)
            if remaining <= 0:
                break
    return rows


def fetch_raw_urls(client: SupabaseRestClient) -> tuple[list[str], list[str]]:
    scrape_rows = client.fetch_rows(TABLE_FIRECRAWL_SCRAPE_RAW, "source_url", limit=5000)
    search_rows = client.fetch_rows(TABLE_FIRECRAWL_SEARCH_RAW, "response_json", limit=5000)
    scrape_urls = [str(row.get("source_url") or "") for row in scrape_rows if row.get("source_url")]
    search_urls: list[str] = []
    for row in search_rows:
        payload = row.get("response_json") or {}
        if not isinstance(payload, dict):
            continue
        for item in payload.get("data") or payload.get("web") or []:
            if isinstance(item, dict) and item.get("url"):
                search_urls.append(str(item["url"]))
    return scrape_urls, search_urls


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=OUTPUT_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_markdown(report: AuditReport, *, paths: Dict[str, Path]) -> str:
    summary = report.as_summary()
    top_types = sorted(
        summary.get("issues_by_type", {}).items(),
        key=lambda item: (-item[1], item[0]),
    )[:15]
    lines = "\n".join(f"- `{kind}`: {count}" for kind, count in top_types)
    return f"""# CostFinder Extraction Quality Audit

- Audited at: `{datetime.now().isoformat()}`
- Tables: `{report.table_counts}`
- Total issues: `{summary['total_issues']}` (blocking/high: `{summary['blocking_issues']}`)

## Top Issue Types
{lines or "- None"}

## Output Files
- Summary JSON: `{paths['summary']}`
- Issues CSV: `{paths['issues']}`
- Canvas data JSON: `{paths.get('canvas', '')}`
"""


def main() -> int:
    args = parse_args()
    report_dir = Path(args.report_dir).expanduser().resolve()
    result_dir = Path(args.result_dir).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    client = load_client()

    services = fetch_all(client, TABLE_CLINIC_SERVICES, CLINIC_SERVICE_SELECT, limit=args.limit)
    memberships = fetch_all(client, TABLE_CLINIC_MEMBERSHIPS, CLINIC_MEMBERSHIP_SELECT, limit=args.limit)
    promotions = fetch_all(client, TABLE_CLINIC_PROMOTIONS, CLINIC_PROMOTION_SELECT, limit=args.limit)
    offers = fetch_all(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_WITH_ITEMS_SELECT, limit=args.limit)
    offer_items = fetch_all(client, TABLE_PROMO_OFFER_ITEMS, OFFER_ITEM_SELECT, limit=args.limit)
    businesses = fetch_all(client, "master_business_info", "business_id,name,website", limit=5000)
    scrape_urls, search_urls = fetch_raw_urls(client)

    report = run_full_audit(
        services=services,
        memberships=memberships,
        promotions=promotions,
        offers=offers,
        offer_items=offer_items,
        businesses=businesses,
        scrape_urls=scrape_urls,
        search_urls=search_urls,
        today=date.today(),
    )

    issue_rows = [issue.as_row() for issue in report.issues]
    summary_path = result_dir / f"extraction_quality_audit_summary_{timestamp}.json"
    issues_path = result_dir / f"extraction_quality_audit_issues_{timestamp}.csv"
    report_path = report_dir / f"extraction_quality_audit_{timestamp}.md"
    canvas_path = Path(args.canvas_data).expanduser().resolve() if args.canvas_data else (
        result_dir / f"extraction_quality_audit_canvas_{timestamp}.json"
    )

    report_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    summary_payload = {
        "audited_at": datetime.now().isoformat(),
        **report.as_summary(),
        "duplicate_groups": {
            key: len(value) for key, value in report.duplicate_groups.items()
        },
    }
    summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(
        issues_path,
        issue_rows,
        ["table", "id", "severity", "issue_type", "detail", "business_name", "label"],
    )
    canvas_payload = {
        "audited_at": summary_payload["audited_at"],
        "table_counts": report.table_counts,
        "summary": report.as_summary(),
        "issues": issue_rows[:200],
        "duplicate_groups": report.duplicate_groups,
    }
    canvas_path.write_text(json.dumps(canvas_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(
        build_markdown(report, paths={"summary": summary_path, "issues": issues_path, "canvas": canvas_path}),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "summary_path": str(summary_path),
                "issues_path": str(issues_path),
                "canvas_data_path": str(canvas_path),
                "report_path": str(report_path),
                "blocking_issues": report.blocking_count,
                "exit_code": 1 if report.blocking_count else 0,
            },
            ensure_ascii=False,
        )
    )
    return 1 if report.blocking_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
