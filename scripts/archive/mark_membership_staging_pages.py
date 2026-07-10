#!/usr/bin/env python3
"""Mark is_membership_page=true on existing promo_website_staging rows."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.membership_paths import is_membership_page_url
from utils.supabase_rest import SupabaseRestClient

TABLE = "promo_website_staging"


def main() -> int:
    parser = argparse.ArgumentParser(description="Mark membership pages in promo_website_staging")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not key:
        print("Missing Supabase credentials", file=sys.stderr)
        return 1

    client = SupabaseRestClient(base_url, key)
    select = "promo_website_id,subpage_url"
    has_flag = True
    try:
        client.fetch_rows(TABLE, "promo_website_id,is_membership_page", limit=1)
        select = "promo_website_id,subpage_url,is_membership_page"
    except Exception:
        has_flag = False
        print("is_membership_page column missing; run config/sql/promo_membership_plans_alter.sql first", file=sys.stderr)

    offset = 0
    marked = 0
    while True:
        batch = client.fetch_rows(
            TABLE,
            select,
            limit=200,
            offset=offset,
            order="promo_website_id.asc",
        )
        if not batch:
            break
        for row in batch:
            url = str(row.get("subpage_url") or "")
            if not is_membership_page_url(url) or (has_flag and row.get("is_membership_page")):
                continue
            marked += 1
            if args.dry_run or not has_flag:
                print(f"would mark {row['promo_website_id']} {url}")
                if not has_flag:
                    continue
                continue
            client.update_row(
                TABLE,
                {"promo_website_id": f"eq.{row['promo_website_id']}"},
                {"is_membership_page": True},
            )
        if len(batch) < 200:
            break
        offset += 200

    print(f"marked={marked} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
