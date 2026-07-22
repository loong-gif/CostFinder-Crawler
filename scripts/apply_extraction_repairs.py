#!/usr/bin/env python3
"""Apply deterministic extraction quality repairs (default dry-run)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.extraction_repair import apply_repair_actions, build_extraction_repair_plan  # noqa: E402
from utils.schema_contract import (  # noqa: E402
    CLINIC_MEMBERSHIP_SELECT,
    CLINIC_PROMOTION_SELECT,
    CLINIC_SERVICE_SELECT,
    OFFER_MASTER_WITH_ITEMS_SELECT,
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_CLINIC_PROMOTIONS,
    TABLE_CLINIC_SERVICES,
    TABLE_FIRECRAWL_SCRAPE_RAW,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply extraction quality repairs")
    parser.add_argument("--apply", action="store_true", help="Execute writes (default dry-run)")
    parser.add_argument("--batch", default="", help="只执行指定 batch")
    parser.add_argument("--result-dir", default=str(PROJECT_ROOT / "output" / "results"))
    return parser.parse_args()


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = get_supabase_writer_key()
    if not base_url or not key:
        raise RuntimeError("缺少 SUPABASE_URL 或 writer key")
    return SupabaseRestClient(base_url, key)


def fetch_rows(client: SupabaseRestClient, table: str, select: str) -> List[Dict[str, Any]]:
    try:
        return client.fetch_rows(table, select, limit=5000)
    except Exception:
        return []


def main() -> int:
    args = parse_args()
    client = load_client()
    services = fetch_rows(client, TABLE_CLINIC_SERVICES, CLINIC_SERVICE_SELECT)
    memberships = fetch_rows(client, TABLE_CLINIC_MEMBERSHIPS, CLINIC_MEMBERSHIP_SELECT)
    promotions = fetch_rows(client, TABLE_CLINIC_PROMOTIONS, CLINIC_PROMOTION_SELECT)
    offers = fetch_rows(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_WITH_ITEMS_SELECT)
    master_rows = fetch_rows(client, "master_business_info", "business_id,name,website")
    staging_rows = fetch_rows(client, "promo_website_staging", "business_id,subpage_url,domain_name")
    scrape_rows = fetch_rows(client, TABLE_FIRECRAWL_SCRAPE_RAW, "source_url,markdown")
    scrape_markdown = {
        str(row.get("source_url") or "").strip().rstrip("/").lower(): str(row.get("markdown") or "")
        for row in scrape_rows
        if row.get("source_url") and row.get("markdown")
    }

    plans = build_extraction_repair_plan(
        services=services,
        memberships=memberships,
        promotions=promotions,
        offers=offers,
        master_rows=master_rows,
        staging_rows=staging_rows,
        scrape_markdown_by_url=scrape_markdown,
    )
    selected = plans.get(args.batch, []) if args.batch else [action for batch in plans.values() for action in batch]
    results = apply_repair_actions(client, selected, dry_run=not args.apply)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_dir = Path(args.result_dir).expanduser().resolve()
    result_dir.mkdir(parents=True, exist_ok=True)
    out_path = result_dir / f"extraction_repair_{timestamp}.json"
    payload = {
        "dry_run": not args.apply,
        "batch_filter": args.batch or None,
        "planned_actions": len(selected),
        "results": results,
        "plan_counts": {name: len(items) for name, items in plans.items()},
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result_path": str(out_path), **payload["plan_counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
