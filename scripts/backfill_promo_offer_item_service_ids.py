#!/usr/bin/env python3
"""Audit or conservatively link promo_offer_items to clinic_services from CSV evidence."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.promo_offer_item_service_backfill import build_backfill_plan
from utils.schema_contract import (
    CLINIC_SERVICE_SELECT,
    OFFER_MASTER_WITH_ITEMS_SELECT,
    TABLE_CLINIC_SERVICES,
    TABLE_PROMO_OFFER_ITEMS,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key, get_supabase_writer_key


def _fetch_all(client: SupabaseRestClient, table: str, select: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(table, select, limit=1000, offset=offset)
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += len(page)


def _load_client(*, apply: bool) -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = str(os.getenv("SUPABASE_URL") or "").strip()
    key = get_supabase_writer_key() if apply else get_supabase_secret_key()
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or required Supabase credential")
    return SupabaseRestClient(base_url, key)


def run(*, csv_path: Path, output: Path, apply: bool) -> dict[str, Any]:
    """Write a reviewable report; mutations require explicit ``apply=True``."""
    client = _load_client(apply=apply)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        csv_rows = list(csv.DictReader(handle))
    offers = _fetch_all(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_WITH_ITEMS_SELECT)
    services = _fetch_all(client, TABLE_CLINIC_SERVICES, CLINIC_SERVICE_SELECT)
    plan = build_backfill_plan(offers, services, csv_rows)
    applied: list[int] = []
    if apply:
        for link in plan.links:
            updated = client.update_row(
                TABLE_PROMO_OFFER_ITEMS,
                {"offer_item_id": f"eq.{link['offer_item_id']}", "service_id": "is.null"},
                {"service_id": link["service_id"]},
            )
            if len(updated) == 1:
                applied.append(int(link["offer_item_id"]))
    unlinked_count = sum(
        1
        for offer in offers
        for item in (offer.get("promo_offer_items") or [])
        if isinstance(item, dict) and item.get("service_id") is None
    )
    report = {
        "source_file": str(csv_path),
        "mode": "apply" if apply else "dry_run",
        "csv_rows": len(csv_rows),
        "offers": len(offers),
        "unlinked_items_before": unlinked_count,
        "safe_links": plan.links,
        "safe_link_count": len(plan.links),
        "skip_counts": dict(sorted(Counter(row["reason"] for row in plan.skipped).items())),
        "skipped": plan.skipped,
        "applied_offer_item_ids": applied,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "promo_offer_item_service_backfill_report.json",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mutate only the reviewed safe links; requires SUPABASE_WRITER_KEY.",
    )
    args = parser.parse_args()
    report = run(csv_path=args.csv_path, output=args.output, apply=args.apply)
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "unlinked_items_before": report["unlinked_items_before"],
                "safe_link_count": report["safe_link_count"],
                "skip_counts": report["skip_counts"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
