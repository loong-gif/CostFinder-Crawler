#!/usr/bin/env python3
"""Build offer evidence segment reports from promo_website_staging.

Default mode is dry-run: fetch staging rows, parse page_content into evidence
segments, and write local artifacts. No Supabase writes happen in this script.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_evidence_segments import build_segment_records, normalize_url, summarize_segment_records

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
TABLE = "promo_website_staging"
SELECT = "promo_website_id,subpage_url,domain_name,name,page_content,processed_status,crawl_timestamp,business_id,membership_context"
PAGE_SIZE = 1000


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
        offset: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"select": select}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        if order:
            params["order"] = order
        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=90)
        response.raise_for_status()
        return response.json()


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate dry-run evidence segments from promo_website_staging")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N staging rows")
    parser.add_argument("--domain", default=None, help="Filter by domain_name")
    parser.add_argument("--id", type=int, dest="row_id", default=None, help="Filter by promo_website_id")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Artifact output directory")
    parser.add_argument("--include-all-segments", action="store_true", help="Include non-offer segments in JSON artifact")
    return parser.parse_args()


def fetch_staging_rows(client: SupabaseRestClient, args: argparse.Namespace) -> List[Dict[str, Any]]:
    filters: Dict[str, str] = {}
    if args.domain:
        filters["domain_name"] = f"eq.{args.domain.strip().lower()}"
    if args.row_id is not None:
        filters["promo_website_id"] = f"eq.{args.row_id}"

    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch_limit = PAGE_SIZE
        if args.limit is not None:
            remaining = args.limit - len(rows)
            if remaining <= 0:
                break
            batch_limit = min(batch_limit, remaining)
        batch = client.fetch_rows(
            TABLE,
            SELECT,
            filters=filters or None,
            limit=batch_limit,
            offset=offset,
            order="promo_website_id.asc",
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < batch_limit:
            break
        offset += len(batch)
        if args.limit is not None and len(rows) >= args.limit:
            break
    return rows


def build_page_result(row: Dict[str, Any], *, include_all_segments: bool) -> Dict[str, Any]:
    records = build_segment_records(row)
    included = records if include_all_segments else [item for item in records if item.get("is_offer_signal")]
    summary = summarize_segment_records(records)
    return {
        "promo_website_id": row.get("promo_website_id"),
        "source_url": row.get("subpage_url"),
        "source_url_normalized": normalize_url(row.get("subpage_url")),
        "domain_name": row.get("domain_name"),
        "name": row.get("name"),
        "business_id": row.get("business_id"),
        "summary": summary,
        "segments": included,
    }


def write_artifacts(output_dir: Path, pages: List[Dict[str, Any]]) -> Dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    json_path = output_dir / f"offer_evidence_segments_{timestamp}.json"
    csv_path = output_dir / f"offer_evidence_segments_{timestamp}_summary.csv"

    total_segments = sum(page["summary"]["segment_count"] for page in pages)
    total_price = sum(page["summary"]["price_signal_count"] for page in pages)
    total_offer = sum(page["summary"]["offer_signal_count"] for page in pages)
    type_counter: Counter[str] = Counter()
    for page in pages:
        for segment_type in page["summary"]["segment_types"]:
            type_counter[segment_type] += 1

    payload = {
        "summary": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "page_count": len(pages),
            "segment_count": total_segments,
            "price_signal_count": total_price,
            "offer_signal_count": total_offer,
            "segment_type_page_counts": dict(sorted(type_counter.items())),
        },
        "pages": pages,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "promo_website_id",
        "domain_name",
        "name",
        "source_url",
        "segment_count",
        "price_signal_count",
        "offer_signal_count",
        "segment_types",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for page in pages:
            summary = page["summary"]
            writer.writerow(
                {
                    "promo_website_id": page.get("promo_website_id"),
                    "domain_name": page.get("domain_name"),
                    "name": page.get("name") or "",
                    "source_url": page.get("source_url"),
                    "segment_count": summary["segment_count"],
                    "price_signal_count": summary["price_signal_count"],
                    "offer_signal_count": summary["offer_signal_count"],
                    "segment_types": ";".join(summary["segment_types"]),
                }
            )
    return {"json_path": str(json_path), "csv_path": str(csv_path)}


def main() -> None:
    args = parse_args()
    client = load_supabase_client()
    rows = fetch_staging_rows(client, args)
    pages = [build_page_result(row, include_all_segments=args.include_all_segments) for row in rows]
    paths = write_artifacts(Path(args.output_dir), pages)
    print(
        json.dumps(
            {
                "status": "dry_run",
                "pages": len(pages),
                "segments": sum(page["summary"]["segment_count"] for page in pages),
                "price_signals": sum(page["summary"]["price_signal_count"] for page in pages),
                "offer_signals": sum(page["summary"]["offer_signal_count"] for page in pages),
                "report_paths": paths,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
