#!/usr/bin/env python3
"""Safely link single-item promo offers to existing or skeleton catalog services.

Only unambiguous canonical service names are used.  Missing catalog entries are
created with identity fields alone, so a promotion price never becomes a
clinic_services.regular_price value.
"""
from __future__ import annotations

import argparse
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

from utils.clinic_services_db import seed_skeleton
from utils.promo_offer_item_service_backfill import build_catalog_backfill_plan
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


def run(*, output: Path, apply: bool) -> dict[str, Any]:
    """Produce an auditable plan; write only when ``apply=True``."""
    client = _load_client(apply=apply)
    offers = _fetch_all(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_WITH_ITEMS_SELECT)
    services = _fetch_all(client, TABLE_CLINIC_SERVICES, CLINIC_SERVICE_SELECT)
    plan = build_catalog_backfill_plan(offers, services)
    service_ids = {
        (int(row["business_id"]), str(row["service_name"])): int(row["service_id"])
        for row in services
        if row.get("business_id") is not None and row.get("service_name") and row.get("service_id") is not None
    }
    created: list[dict[str, Any]] = []
    linked: list[int] = []
    failures: list[dict[str, Any]] = []

    if apply:
        for create in plan.creates:
            key = (int(create["business_id"]), str(create["service_name"]))
            try:
                row = seed_skeleton(client, *key)
                service_ids[key] = int(row["service_id"])
                created.append({**create, "service_id": service_ids[key]})
            except Exception as exc:  # report each failure without applying an unsafe link
                failures.append({**create, "stage": "create", "error": str(exc)})

        for link in plan.links:
            service_id = link.get("service_id") or service_ids.get((int(link["business_id"]), str(link["service_name"])))
            if service_id is None:
                failures.append({**link, "stage": "link", "error": "service_not_available"})
                continue
            try:
                updated = client.update_row(
                    TABLE_PROMO_OFFER_ITEMS,
                    {"offer_item_id": f"eq.{link['offer_item_id']}", "service_id": "is.null"},
                    {"service_id": int(service_id)},
                )
                if len(updated) == 1:
                    linked.append(int(link["offer_item_id"]))
            except Exception as exc:
                failures.append({**link, "stage": "link", "error": str(exc)})

    unlinked_before = sum(
        1
        for offer in offers
        for item in (offer.get("promo_offer_items") or [])
        if isinstance(item, dict) and item.get("service_id") is None
    )
    report = {
        "mode": "apply" if apply else "dry_run",
        "offers": len(offers),
        "unlinked_items_before": unlinked_before,
        "planned_creates": plan.creates,
        "planned_create_count": len(plan.creates),
        "planned_links": plan.links,
        "planned_link_count": len(plan.links),
        "skip_counts": dict(sorted(Counter(row["reason"] for row in plan.skipped).items())),
        "skipped": plan.skipped,
        "created_services": created,
        "linked_offer_item_ids": linked,
        "failures": failures,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "promo_offer_item_catalog_service_backfill_report.json",
    )
    parser.add_argument("--apply", action="store_true", help="Create skeleton services and link reviewed items.")
    args = parser.parse_args()
    report = run(output=args.output, apply=args.apply)
    print(json.dumps({
        "mode": report["mode"],
        "unlinked_items_before": report["unlinked_items_before"],
        "planned_create_count": report["planned_create_count"],
        "planned_link_count": report["planned_link_count"],
        "skip_counts": report["skip_counts"],
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
