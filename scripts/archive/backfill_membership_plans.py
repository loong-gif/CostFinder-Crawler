#!/usr/bin/env python3
"""Backfill promo_membership_plans from /membership staging pages.

Reads promo_website_staging rows whose subpage_url contains membership paths,
LLM-extracts tier structure, writes promo_membership_plans + linked offers,
and soft-ends mis-filed membership offers on those pages.

Usage:
    python scripts/backfill_membership_plans.py --dry-run --limit 3
    python scripts/backfill_membership_plans.py --id 123
    python scripts/backfill_membership_plans.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.membership_paths import is_membership_page_url
from utils.membership_plans import (
    extract_membership_plans_for_row,
    persist_membership_extraction,
)
from utils.offer_extraction_llm import build_client_from_env
from utils.supabase_rest import SupabaseRestClient

TABLE = "promo_website_staging"
PAGE_SIZE = 100
SELECT = (
    "promo_website_id,subpage_url,domain_name,page_content,"
    "crawl_timestamp,business_id"
)


def staging_select(client: SupabaseRestClient) -> str:
    try:
        client.fetch_rows(TABLE, SELECT + ",is_membership_page", limit=1)
        return SELECT + ",is_membership_page"
    except Exception:
        return SELECT
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
REPORT_PREFIX = "backfill_membership_plans"


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill membership plans from staging pages")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N rows")
    parser.add_argument("--domain", default=None, help="Filter by domain_name")
    parser.add_argument("--id", type=int, dest="row_id", default=None, help="Filter by promo_website_id")
    parser.add_argument("--dry-run", action="store_true", help="Extract only; do not write to Supabase")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Report output directory")
    parser.add_argument("--mark-staging", action="store_true", help="Set is_membership_page=true on processed rows")
    return parser.parse_args()


def table_exists(client: SupabaseRestClient, table: str) -> bool:
    try:
        client.fetch_rows(table, "plan_id" if table == "promo_membership_plans" else "id", limit=1)
        return True
    except Exception:
        return False


def fetch_membership_staging_rows(
    client: SupabaseRestClient,
    args: argparse.Namespace,
) -> List[Dict[str, Any]]:
    filters: Dict[str, str] = {}
    if args.domain:
        filters["domain_name"] = f"eq.{args.domain.strip().lower()}"
    if args.row_id is not None:
        filters["promo_website_id"] = f"eq.{args.row_id}"

    rows: List[Dict[str, Any]] = []
    offset = 0
    select = staging_select(client)
    while True:
        batch_limit = PAGE_SIZE
        if args.limit is not None:
            remaining = args.limit - len(rows)
            if remaining <= 0:
                break
            batch_limit = min(batch_limit, remaining)

        batch = client.fetch_rows(
            TABLE,
            select,
            filters=filters or None,
            limit=batch_limit,
            offset=offset,
            order="promo_website_id.asc",
        )
        if not batch:
            break

        for row in batch:
            url = str(row.get("subpage_url") or "")
            if is_membership_page_url(url):
                rows.append(row)

        if len(batch) < batch_limit:
            break
        offset += batch_limit

        if args.limit is not None and len(rows) >= args.limit:
            rows = rows[: args.limit]
            break

    return rows


def mark_membership_page(client: SupabaseRestClient, row: Dict[str, Any], *, dry_run: bool) -> None:
    if dry_run or row.get("is_membership_page"):
        return
    try:
        client.update_row(
            TABLE,
            {"promo_website_id": f"eq.{row['promo_website_id']}"},
            {"is_membership_page": True},
        )
    except Exception:
        # Column may not exist until migration is applied.
        pass


def process_row(
    row: Dict[str, Any],
    *,
    llm_client: Any,
    sb_client: SupabaseRestClient,
    dry_run: bool,
    mark_staging: bool,
) -> Dict[str, Any]:
    url = str(row.get("subpage_url") or "")
    result: Dict[str, Any] = {
        "promo_website_id": row.get("promo_website_id"),
        "subpage_url": url,
        "domain_name": row.get("domain_name"),
        "plans_extracted": 0,
        "plans_inserted": 0,
        "offers_inserted": 0,
        "offers_ended": 0,
        "error": "",
    }

    content = str(row.get("page_content") or "").strip()
    if len(content) < 40:
        result["error"] = "empty_or_short_page_content"
        return result

    try:
        plans = extract_membership_plans_for_row(row, client=llm_client, page_content=content)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"llm_error:{exc}"
        return result

    result["plans_extracted"] = len(plans)
    if not plans:
        result["error"] = "no_plans_extracted"
        return result

    try:
        persist_result = persist_membership_extraction(
            sb_client,
            row,
            plans,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"persist_error:{exc}"
        return result

    result.update(
        {
            "plans_inserted": persist_result["plans_inserted"],
            "offers_inserted": persist_result["offers_inserted"],
            "offers_ended": persist_result["offers_ended"],
            "plans_preview": persist_result.get("plans") or [],
        }
    )

    if mark_staging:
        mark_membership_page(sb_client, row, dry_run=dry_run)

    return result


def main() -> int:
    args = parse_args()
    sb_client = load_supabase_client()
    llm_client = build_client_from_env()
    if llm_client is None:
        print("Missing LLM_API_URL / LLM_MODEL / LLM_API_KEY", file=sys.stderr)
        return 1

    if not table_exists(sb_client, "promo_membership_plans"):
        print(
            "Table promo_membership_plans not found. "
            "Apply config/sql/promo_membership_plans.sql first "
            "(Supabase SQL Editor or scripts/apply_sql_migration.py).",
            file=sys.stderr,
        )
        return 2

    rows = fetch_membership_staging_rows(sb_client, args)
    results = [
        process_row(
            row,
            llm_client=llm_client,
            sb_client=sb_client,
            dry_run=args.dry_run,
            mark_staging=args.mark_staging,
        )
        for row in rows
    ]

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "rows_scanned": len(rows),
        "rows_with_plans": sum(1 for item in results if item.get("plans_extracted")),
        "plans_inserted": sum(item.get("plans_inserted", 0) for item in results),
        "offers_inserted": sum(item.get("offers_inserted", 0) for item in results),
        "offers_ended": sum(item.get("offers_ended", 0) for item in results),
        "errors": sum(1 for item in results if item.get("error")),
        "results": results,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"{REPORT_PREFIX}_{stamp}.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=2))
    print(f"Report: {report_path}")
    return 0 if summary["errors"] < len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
