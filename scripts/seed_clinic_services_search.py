#!/usr/bin/env python3
"""Fill or refresh clinic_services via Firecrawl Cloud Search + unit-price extraction.

Usage:
    python scripts/seed_clinic_services_search.py --business-id 51 --dry-run
    python scripts/seed_clinic_services_search.py --limit 20 --apply
    python scripts/seed_clinic_services_search.py --refresh-only --older-than-days 30 --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import CLINIC_SERVICES_FALLBACK_CRAWL_PAGES, OUTPUT_DIR
from crawler.promo_site_crawler import is_filtered_process_flag
from crawler.staging_recrawl import fetch_all_rows, load_supabase_client
from scripts.seed_clinic_services_botox import crawl_website
from utils.clinic_services_botox import (
    extract_botox_fields_from_pages,
    extract_botox_fields_from_search_pages,
    website_to_crawl_url,
)
from utils.clinic_services_db import apply_fields, seed_skeleton
from utils.clinic_services_search import business_base_domain, search_service_pages
from utils.logger import log

DEFAULT_SERVICE = "Botox"
REPORT_PREFIX = "clinic_services_search"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill clinic_services via Firecrawl Search.")
    parser.add_argument("--service", default=DEFAULT_SERVICE, help="Service name (default: Botox)")
    parser.add_argument("--limit", type=int, default=None, help="Max businesses to process")
    parser.add_argument("--business-id", type=int, default=None, help="Single business_id")
    parser.add_argument("--dry-run", action="store_true", help="Search/extract only; no DB writes")
    parser.add_argument("--apply", action="store_true", help="Write seed/update to clinic_services")
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help="Only businesses that already have a clinic_services row",
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=None,
        help="With --refresh-only: refresh rows older than N days or missing price",
    )
    parser.add_argument(
        "--fallback-crawl",
        action="store_true",
        help="If search finds no price, fall back to self-hosted full-site crawl",
    )
    parser.add_argument("--output", type=Path, default=None, help="JSON report path")
    return parser.parse_args()


def list_businesses(client, *, business_id: Optional[int]) -> List[Dict[str, Any]]:
    filters = {"business_id": f"eq.{business_id}"} if business_id is not None else None
    try:
        rows = fetch_all_rows(
            client,
            "master_business_info",
            "business_id,name,website,process_flag",
            filters=filters,
            order="business_id.asc",
        )
    except Exception:
        rows = fetch_all_rows(
            client,
            "master_business_info",
            "business_id,name,website",
            filters=filters,
            order="business_id.asc",
        )
    out: List[Dict[str, Any]] = []
    for row in rows:
        if is_filtered_process_flag(row.get("process_flag")):
            continue
        if row.get("business_id") is None:
            continue
        out.append(row)
    return out


def extract_fields_for_service(service_name: str, pages: List[Any]):
    if service_name == "Botox":
        return extract_botox_fields_from_search_pages(pages)
    raise ValueError(f"Unsupported service for extraction: {service_name}")


def fallback_crawl_extract(website: str, service_name: str):
    if service_name != "Botox":
        return extract_botox_fields_from_pages([])
    pages = crawl_website(
        website_to_crawl_url(website),
        max_pages=CLINIC_SERVICES_FALLBACK_CRAWL_PAGES,
        timeout_secs=600,
    )
    return extract_botox_fields_from_pages(pages)


def process_business(
    business: Dict[str, Any],
    *,
    service_name: str,
    apply: bool,
    client,
    fallback_crawl: bool,
) -> Dict[str, Any]:
    bid = int(business["business_id"])
    website = business.get("website")
    domain = business_base_domain(website)
    report: Dict[str, Any] = {
        "business_id": bid,
        "name": business.get("name") or "",
        "website": website_to_crawl_url(website),
        "domain": domain,
        "service_name": service_name,
        "queries_run": [],
        "pages_found": 0,
        "pages_used": 0,
        "regular_price": None,
        "unit_type": None,
        "service_area": None,
        "status": "pending",
        "error": "",
        "source_urls": [],
    }
    if not domain:
        report["status"] = "no_website"
        return report

    try:
        pages, queries = search_service_pages(website, service_name)
        report["queries_run"] = queries
        report["pages_found"] = len(pages)
        report["pages_used"] = len(pages)
        report["source_urls"] = [p.url for p in pages]

        fields = extract_fields_for_service(service_name, pages)
        if fields.regular_price is None and fallback_crawl:
            fields = fallback_crawl_extract(website, service_name)
            report["status"] = "fallback_crawl"

        if fields.regular_price is not None:
            report["regular_price"] = float(fields.regular_price)
            report["unit_type"] = fields.unit_type
            report["service_area"] = fields.service_area
            report["status"] = report.get("status") or "ok"
        else:
            report["status"] = "no_price"

        if apply and fields.regular_price is not None:
            row = seed_skeleton(client, bid, service_name)
            apply_fields(
                client,
                int(row["service_id"]),
                fields,
                existing_price=row.get("regular_price"),
            )
            report["service_id"] = row["service_id"]
        elif apply and report["status"] == "no_price":
            seed_skeleton(client, bid, service_name)
    except Exception as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        log.warning(
            "clinic_services search failed business_id={bid}: {err}".format(bid=bid, err=exc)
        )
    return report


def main() -> None:
    args = parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("Use only one of --apply or --dry-run")
    apply = bool(args.apply)
    load_dotenv(PROJECT_ROOT / ".env")
    client = load_supabase_client()

    if args.refresh_only:
        existing = fetch_all_rows(
            client,
            "clinic_services",
            "business_id,service_name,regular_price,updated_at",
            filters={"service_name": f"eq.{args.service}"},
            order="business_id.asc",
        )
        if args.older_than_days is not None:
            from utils.clinic_services_db import fetch_rows_for_refresh

            existing = fetch_rows_for_refresh(
                client,
                service_name=args.service,
                older_than_days=args.older_than_days,
                business_id=args.business_id,
            )
        business_ids = {int(r["business_id"]) for r in existing}
        businesses = [
            b
            for b in list_businesses(client, business_id=args.business_id)
            if int(b["business_id"]) in business_ids
        ]
    else:
        businesses = list_businesses(client, business_id=args.business_id)
        businesses = [b for b in businesses if website_to_crawl_url(b.get("website"))]
        if args.limit is not None:
            businesses = businesses[: args.limit]

    if not businesses:
        raise RuntimeError("No businesses to process")

    reports = [
        process_business(
            b,
            service_name=args.service,
            apply=apply,
            client=client,
            fallback_crawl=args.fallback_crawl,
        )
        for b in businesses
    ]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = args.output or (OUTPUT_DIR / f"{REPORT_PREFIX}_{stamp}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "report_path": str(out_path),
        "service": args.service,
        "dry_run": not apply,
        "refresh_only": args.refresh_only,
        "business_count": len(businesses),
        "ok": sum(1 for r in reports if r.get("status") == "ok"),
        "no_price": sum(1 for r in reports if r.get("status") == "no_price"),
        "no_website": sum(1 for r in reports if r.get("status") == "no_website"),
        "error": sum(1 for r in reports if r.get("status") == "error"),
        "fallback_crawl": sum(1 for r in reports if r.get("status") == "fallback_crawl"),
        "rows": reports,
    }
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, indent=2))
    print(f"report_path={out_path}")


if __name__ == "__main__":
    main()
