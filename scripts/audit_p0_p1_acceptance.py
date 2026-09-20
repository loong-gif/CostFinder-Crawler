#!/usr/bin/env python3
"""Run the hard P0/P1 acceptance gates against the live Supabase REST API."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.p0_p1_acceptance import acceptance_gate_counts, acceptance_passes
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key


def fetch_all(client: SupabaseRestClient, table: str, select: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(table, select, limit=1000, offset=offset)
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += len(page)


def build_report(client: SupabaseRestClient) -> dict[str, Any]:
    offers = fetch_all(
        client,
        "promo_offer_master",
        "id,promotion_id,is_active,price_model,discount_price,discount_amount,regular_price,offer_raw_text,promo_offer_items(offer_item_id,item_name,service_id,unit_price),clinic_promotions(promotion_id,is_active)",
    )
    services = fetch_all(client, "clinic_services", "service_id,service_name")
    promotions: dict[Any, dict[str, Any]] = {}
    for offer in offers:
        relation = offer.get("clinic_promotions") or {}
        if isinstance(relation, list):
            relation = relation[0] if relation else {}
        if relation.get("promotion_id") is not None:
            promotions[relation["promotion_id"]] = relation
    service_map = {row["service_id"]: row for row in services if row.get("service_id") is not None}
    counts = acceptance_gate_counts(offers, promotions, service_map)
    missing_regular = [
        row["id"]
        for row in offers
        if row.get("is_active")
        and row.get("discount_price") is not None
        and row.get("discount_amount") is not None
        and row.get("regular_price") is None
    ]
    return {
        "offers_audited": len(offers),
        "hard_gates": counts,
        "price_completeness_missing_regular_price": len(missing_regular),
        "missing_regular_price_offer_ids": missing_regular,
        # PostgREST cannot inspect pg_proc/ACL metadata. Keep this explicitly
        # unknown until the SQL migration or a metadata query proves it.
        "security_definer_without_internal_authorization": None,
        "security_gate_status": "not_verified_without_sql_metadata_access",
        "data_gates_pass": acceptance_passes(counts),
        "final_acceptance": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = get_supabase_writer_key()
    if not url or not key:
        raise SystemExit("缺少 SUPABASE_URL 或 writer key")
    report = build_report(SupabaseRestClient(url, key))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
