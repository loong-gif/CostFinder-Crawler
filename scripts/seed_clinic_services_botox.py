#!/usr/bin/env python3
"""Crawl business websites via Firecrawl and export Botox unit prices to CSV (no DB writes).

Reads master_business_info for business_id/name/website only.
Writes output/clinic_services_botox_*.csv.

Usage:
    python scripts/seed_clinic_services_botox.py --business-id 51
    python scripts/seed_clinic_services_botox.py --limit 3
    python scripts/seed_clinic_services_botox.py --seed-only   # CSV skeleton only, no crawl
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import FIRECRAWL_CRAWL_MAX_PAGES, FIRECRAWL_CRAWL_TIMEOUT_SECS, OUTPUT_DIR
from crawler.promo_site_crawler import is_filtered_process_flag
from crawler.staging_recrawl import _crawl_documents_to_items, fetch_all_rows, load_supabase_client
from firecrawl.v2.types import ScrapeOptions
from utils.clinic_services_botox import extract_botox_fields_from_pages, website_to_crawl_url
from utils.firecrawl_client import get_firecrawl_client
from utils.logger import log

SERVICE_NAME = "Botox"
CSV_PREFIX = "clinic_services_botox"
CSV_FIELDS = [
    "business_id",
    "name",
    "website",
    "service_name",
    "regular_price",
    "unit_type",
    "service_area",
    "crawl_pages",
    "status",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl websites for Botox unit prices and write CSV (does not write Supabase)."
    )
    parser.add_argument("--limit", type=int, default=None, help="Only crawl first N businesses with website")
    parser.add_argument("--business-id", type=int, default=None, help="Only one business_id")
    parser.add_argument("--max-crawl-pages", type=int, default=FIRECRAWL_CRAWL_MAX_PAGES)
    parser.add_argument("--crawl-timeout-secs", type=int, default=FIRECRAWL_CRAWL_TIMEOUT_SECS)
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Write CSV skeleton rows only (service_name=Botox, prices empty); no crawl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="CSV path (default: output/clinic_services_botox_<timestamp>.csv)",
    )
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
        # ponytail: some envs lack process_flag
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


def crawl_website(website_url: str, *, max_pages: int, timeout_secs: int) -> List[Dict[str, Any]]:
    fc = get_firecrawl_client()
    crawl_job = fc.crawl(
        website_url,
        limit=max_pages,
        scrape_options=ScrapeOptions(formats=["markdown"], only_main_content=True, block_ads=True),
        allow_subdomains=True,
        ignore_query_parameters=True,
        timeout=timeout_secs,
    )
    documents = getattr(crawl_job, "data", None) or []
    return _crawl_documents_to_items(documents)


def skeleton_row(business: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "business_id": int(business["business_id"]),
        "name": business.get("name") or "",
        "website": website_to_crawl_url(business.get("website")),
        "service_name": SERVICE_NAME,
        "regular_price": "",
        "unit_type": "",
        "service_area": "",
        "crawl_pages": "",
        "status": "seeded",
        "error": "",
    }


def crawl_row(
    business: Dict[str, Any],
    *,
    max_pages: int,
    timeout_secs: int,
) -> Dict[str, Any]:
    row = skeleton_row(business)
    website_url = row["website"]
    if not website_url:
        row["status"] = "no_website"
        return row
    try:
        pages = crawl_website(website_url, max_pages=max_pages, timeout_secs=timeout_secs)
        fields = extract_botox_fields_from_pages(pages)
        row["crawl_pages"] = len(pages)
        if fields.regular_price is not None:
            row["regular_price"] = float(fields.regular_price)
        row["unit_type"] = fields.unit_type or ""
        row["service_area"] = fields.service_area or ""
        row["status"] = "ok" if fields.regular_price is not None else "no_price"
    except Exception as exc:
        row["status"] = "error"
        row["error"] = str(exc)
        log.warning(
            "clinic_services botox crawl failed business_id={bid}: {err}".format(
                bid=row["business_id"], err=exc
            )
        )
    return row


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    client = load_supabase_client()

    businesses = list_businesses(client, business_id=args.business_id)
    if not businesses:
        raise RuntimeError("No master_business_info rows to process")

    if args.seed_only:
        rows = [skeleton_row(b) for b in businesses]
    else:
        targets = [b for b in businesses if website_to_crawl_url(b.get("website"))]
        if args.limit is not None:
            targets = targets[: args.limit]
        # include no-website businesses as skeleton when not limiting / single id
        if args.business_id is not None or args.limit is None:
            no_web = [b for b in businesses if not website_to_crawl_url(b.get("website"))]
        else:
            no_web = []
        rows = [
            crawl_row(b, max_pages=args.max_crawl_pages, timeout_secs=args.crawl_timeout_secs)
            for b in targets
        ]
        rows.extend(skeleton_row(b) | {"status": "no_website"} for b in no_web)
        rows.sort(key=lambda r: int(r["business_id"]))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = args.output or (OUTPUT_DIR / f"{CSV_PREFIX}_{stamp}.csv")
    write_csv(csv_path, rows)

    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "csv_path": str(csv_path),
        "business_count": len(businesses),
        "row_count": len(rows),
        "seed_only": bool(args.seed_only),
        "ok": sum(1 for r in rows if r.get("status") == "ok"),
        "no_price": sum(1 for r in rows if r.get("status") == "no_price"),
        "no_website": sum(1 for r in rows if r.get("status") == "no_website"),
        "error": sum(1 for r in rows if r.get("status") == "error"),
        "seeded": sum(1 for r in rows if r.get("status") == "seeded"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=lambda v: float(v) if isinstance(v, Decimal) else v))
    print(f"csv_path={csv_path}")


if __name__ == "__main__":
    main()
