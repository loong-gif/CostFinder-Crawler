#!/usr/bin/env python3
"""Plan or apply a deduplicated CSV import into promo_offer_master/items."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.promo_csv_import import build_import_plan, normalize_source_url
from utils.schema_contract import TABLE_CLINIC_PROMOTIONS, TABLE_MASTER_BUSINESS, TABLE_PROMO_OFFER_ITEMS, TABLE_PROMO_OFFER_MASTER
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
    base_url = (os.getenv("SUPABASE_URL") or "").strip()
    key = get_supabase_writer_key() if apply else get_supabase_secret_key()
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or required Supabase credential")
    return SupabaseRestClient(base_url, key)


def _preflight(client: SupabaseRestClient) -> None:
    master = client.fetch_rows(TABLE_PROMO_OFFER_MASTER, "*", limit=1)
    items = client.fetch_rows(TABLE_PROMO_OFFER_ITEMS, "*", limit=1)
    if master:
        required_master = {"id", "business_id", "promotion_id", "offer_fingerprint", "is_active", "price_model"}
        missing = required_master - set(master[0])
        if missing:
            raise RuntimeError(f"promo_offer_master schema mismatch: missing {sorted(missing)}")
    if items:
        required_items = {"offer_item_id", "offer_id", "quantity", "unit_price"}
        missing = required_items - set(items[0])
        if missing:
            raise RuntimeError(f"promo_offer_items schema mismatch: missing {sorted(missing)}")


def _promotion_payload(business_id: int, source_url: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "business_id": business_id,
        "source_url": source_url,
        "promotion_title": "Imported CSV promotion",
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }


def _write_offer(client: SupabaseRestClient, master: dict[str, Any], items: list[dict[str, Any]]) -> int:
    inserted = client.insert_rows(TABLE_PROMO_OFFER_MASTER, [master])
    offer_id = int(inserted[0]["id"])
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"offer_id": offer_id, "created_at": now, "updated_at": now, **item} for item in items] or [
        {"offer_id": offer_id, "created_at": now, "updated_at": now}
    ]
    try:
        client.insert_rows(TABLE_PROMO_OFFER_ITEMS, rows)
    except Exception:
        client.delete_rows(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{offer_id}"})
        raise
    return offer_id


def run(csv_path: Path, *, output: Path, apply: bool) -> dict[str, Any]:
    client = _load_client(apply=apply)
    _preflight(client)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        csv_rows = list(csv.DictReader(handle))
    businesses = _fetch_all(client, TABLE_MASTER_BUSINESS, "business_id,name,website")
    promotions = _fetch_all(client, TABLE_CLINIC_PROMOTIONS, "promotion_id,business_id,source_url")
    services = _fetch_all(client, "clinic_services", "service_id,business_id,service_name")
    existing = _fetch_all(client, TABLE_PROMO_OFFER_MASTER, "id,offer_fingerprint")
    promotions_by_url = {
        normalize_source_url(row.get("source_url")): row
        for row in promotions
        if normalize_source_url(row.get("source_url"))
    }
    businesses_by_domain: dict[str, list[dict[str, Any]]] = {}
    businesses_by_name: dict[str, list[dict[str, Any]]] = {}
    from utils.promo_csv_import import source_domain

    for business in businesses:
        domain = source_domain(business.get("website"))
        if domain:
            businesses_by_domain.setdefault(domain, []).append(business)
        name = " ".join(str(business.get("name") or "").split()).casefold()
        if name:
            businesses_by_name.setdefault(name, []).append(business)
    plan = build_import_plan(
        csv_rows,
        promotions_by_url=promotions_by_url,
        businesses_by_domain=businesses_by_domain,
        businesses_by_name=businesses_by_name,
    )
    existing_fingerprints = {str(row.get("offer_fingerprint") or "") for row in existing}
    service_ids = {
        (int(row["business_id"]), str(row.get("service_name") or "")): int(row["service_id"])
        for row in services
        if row.get("business_id") is not None and row.get("service_id") is not None
    }
    unresolved_services: list[dict[str, Any]] = []
    resolved_entries = []
    for entry in plan.accepted:
        items = []
        for item in entry.items:
            service_name = str(item.get("service_name") or "")
            service_id = service_ids.get((int(entry.master["business_id"]), service_name))
            if service_id is None:
                unresolved_services.append({"source_row": entry.source_row, "business_id": entry.master["business_id"], "service_name": service_name})
                items = []
                break
            items.append({key: value for key, value in item.items() if key != "service_name"} | {"service_id": service_id})
        if items:
            resolved_entries.append((entry, items))
    eligible = [(entry, items) for entry, items in resolved_entries if entry.master["offer_fingerprint"] not in existing_fingerprints]
    duplicates = len(plan.accepted) - len(eligible)
    report: dict[str, Any] = {
        "source_file": str(csv_path),
        "mode": "apply" if apply else "dry_run",
        "source_rows": len(csv_rows),
        "planned_rows": len(plan.accepted),
        "existing_fingerprint_duplicates": duplicates,
        "eligible_rows": len(eligible),
        "unresolved_services": unresolved_services,
        "rejected": [rejection.__dict__ for rejection in plan.rejected],
        "written_offer_ids": [],
    }
    if apply:
        promotion_cache = dict(promotions_by_url)
        for entry, items in eligible:
            master = dict(entry.master)
            url = normalize_source_url(csv_rows[entry.source_row - 2]["source_url"])
            if entry.needs_promotion:
                promotion = promotion_cache.get(url)
                if promotion is None:
                    created = client.insert_rows(TABLE_CLINIC_PROMOTIONS, [_promotion_payload(master["business_id"], url)])[0]
                    promotion_cache[url] = created
                    promotion = created
                master["promotion_id"] = int(promotion["promotion_id"])
            report["written_offer_ids"].append(_write_offer(client, master, items))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "outputs" / "promo_csv_import_report.json")
    parser.add_argument("--apply", action="store_true", help="Requires SUPABASE_WRITER_KEY; without it, performs read-only dry-run.")
    args = parser.parse_args()
    report = run(args.csv_path, output=args.output, apply=args.apply)
    print(json.dumps({key: value for key, value in report.items() if key not in {"rejected", "unresolved_services", "written_offer_ids"}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
