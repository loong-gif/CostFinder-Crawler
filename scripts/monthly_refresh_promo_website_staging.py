#!/usr/bin/env python3
"""
Monthly refresh for promo_website_staging using Firecrawl crawl API.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import FIRECRAWL_CRAWL_MAX_PAGES, FIRECRAWL_CRAWL_TIMEOUT_SECS, OUTPUT_DIR
from crawler.promo_site_crawler import normalize_domain
from crawler.staging_recrawl import (
    SupabaseRestClient,
    fetch_all_rows,
    load_supabase_client,
    scrape_subpages_for_domain,
)

REPORT_PREFIX = "monthly_promo_website_refresh"
SKIP_REPORT_PREFIX = "monthly_promo_website_refresh_skip"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按月调用 Firecrawl crawl 刷新 promo_website_staging.page_content")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个域名")
    parser.add_argument("--domain", default=None, help="只处理指定域名")
    parser.add_argument("--max-crawl-pages", type=int, default=FIRECRAWL_CRAWL_MAX_PAGES, help="单站 Firecrawl 最大抓取页数")
    parser.add_argument(
        "--crawl-timeout-secs",
        type=int,
        default=FIRECRAWL_CRAWL_TIMEOUT_SECS,
        help="单站 Firecrawl crawl 超时时间",
    )
    parser.add_argument("--dry-run", action="store_true", help="只抓取并生成报告，不写回 Supabase")
    parser.add_argument(
        "--once-per-month",
        action="store_true",
        help="如果本月已存在成功报告，则直接跳过，适合挂到每周自动化里",
    )
    return parser.parse_args()


def resolve_report_path(prefix: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"{prefix}_{timestamp}.json"


def has_completed_report_for_current_month() -> bool:
    month_prefix = datetime.now().strftime("%Y%m")
    pattern = f"{REPORT_PREFIX}_{month_prefix}*.json"
    for path in OUTPUT_DIR.glob(pattern):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("status") == "completed":
            return True
    return False


def list_target_domains(client: SupabaseRestClient, *, domain: Optional[str], limit: Optional[int]) -> List[str]:
    filters = {"domain_name": "not.is.null"}
    if domain:
        filters = {"domain_name": f"eq.{normalize_domain(domain)}"}
    rows = fetch_all_rows(client, "promo_website_staging", "domain_name", filters=filters, order="domain_name.asc")
    domains: List[str] = []
    seen = set()
    for row in rows:
        normalized = normalize_domain(row.get("domain_name"))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        domains.append(normalized)
    if limit is not None:
        domains = domains[:limit]
    return domains


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.once_per_month and has_completed_report_for_current_month():
        report_path = resolve_report_path(SKIP_REPORT_PREFIX)
        report = {
            "status": "skipped_existing_month_report",
            "dry_run": bool(args.dry_run),
            "limit": args.limit,
            "domain": args.domain,
            "message": "本月已经存在成功执行报告，跳过刷新。",
        }
        write_json(report_path, report)
        print(json.dumps({"report_path": str(report_path), **report}, ensure_ascii=False, indent=2))
        return

    client = load_supabase_client()
    domains = list_target_domains(client, domain=args.domain, limit=args.limit)
    if not domains:
        raise RuntimeError("没有可处理的目标域名")

    domain_reports: List[Dict[str, Any]] = []
    total_crawl_rows = 0
    total_errors = 0
    total_updates = 0
    total_inserts = 0

    for index, domain in enumerate(domains, start=1):
        try:
            result = scrape_subpages_for_domain(
                domain,
                client=client,
                dry_run=bool(args.dry_run),
            )
            upsert = result.get("upsert") or {}
            total_crawl_rows += int(upsert.get("crawl_rows") or result.get("hit_pages") or 0)
            total_updates += int(upsert.get("updated_rows") or 0)
            total_inserts += int(upsert.get("inserted_rows") or 0)
            domain_reports.append({"status": "ok", "index": index, "domain": domain, **result})
        except Exception as exc:
            total_errors += 1
            domain_reports.append({"status": "error", "index": index, "domain": domain, "error": str(exc)})

    report_path = resolve_report_path(REPORT_PREFIX)
    summary = {
        "status": "completed",
        "dry_run": bool(args.dry_run),
        "engine": "firecrawl",
        "limit": args.limit,
        "domain": normalize_domain(args.domain) if args.domain else None,
        "target_domains": len(domains),
        "error_domains": total_errors,
        "total_crawl_rows": total_crawl_rows,
        "updated_rows": total_updates,
        "inserted_rows": total_inserts,
        "report_path": str(report_path),
    }
    write_json(report_path, {**summary, "domains": domain_reports})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
