#!/usr/bin/env python3
"""Detach a reviewed membership offer from an unsupported catalog service.

The membership offer and its raw text remain intact.  The catalog entry is
deleted only after the one approved item link has been detached and all current
row values still match the reviewed preconditions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.agent_defense import audit_then_delete_rows, script_agent_name  # noqa: E402
from utils.schema_contract import (  # noqa: E402
    CLINIC_SERVICE_SELECT,
    OFFER_ITEM_SELECT,
    OFFER_MASTER_SELECT,
    TABLE_CLINIC_SERVICES,
    TABLE_PROMO_OFFER_ITEMS,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402


TARGET = {
    "service_id": 3011,
    "business_id": 1650,
    "service_name": "Letybo",
    "regular_price": None,
    "source_url": "https://boulderskinbar.com/memberships",
    "offer_item_id": 3013,
    "offer_id": 3073,
    "membership_plan_id": 238,
    "offer_raw_text": "$11 per unit Botox, Xeomin, Dysport & Letybo",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="execute guarded detach + delete")
    parser.add_argument("--result-dir", type=Path, default=PROJECT_ROOT / "output" / "results")
    return parser.parse_args()


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = get_supabase_writer_key()
    if not base_url or not key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_WRITER_KEY")
    return SupabaseRestClient(base_url, key)


def service_matches(row: Mapping[str, Any], target: Mapping[str, Any] = TARGET) -> bool:
    return (
        row.get("service_id") == target["service_id"]
        and row.get("business_id") == target["business_id"]
        and str(row.get("service_name") or "").strip() == target["service_name"]
        and row.get("regular_price") is None
        and str(row.get("source_url") or "").strip() == target["source_url"]
    )


def offer_matches(row: Mapping[str, Any], target: Mapping[str, Any] = TARGET) -> bool:
    return (
        row.get("id") == target["offer_id"]
        and row.get("business_id") == target["business_id"]
        and row.get("membership_plan_id") == target["membership_plan_id"]
        and row.get("is_active") is True
        and str(row.get("offer_raw_text") or "").strip() == target["offer_raw_text"]
    )


def item_matches(row: Mapping[str, Any], target: Mapping[str, Any] = TARGET) -> bool:
    return (
        row.get("offer_item_id") == target["offer_item_id"]
        and row.get("offer_id") == target["offer_id"]
        and row.get("service_id") == target["service_id"]
    )


def run(client: Any, *, apply: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {"service_id": TARGET["service_id"], "applied": False}
    service_rows = client.fetch_rows(
        TABLE_CLINIC_SERVICES, CLINIC_SERVICE_SELECT,
        filters={"service_id": f"eq.{TARGET['service_id']}"}, limit=1,
    )
    item_rows = client.fetch_rows(
        TABLE_PROMO_OFFER_ITEMS, OFFER_ITEM_SELECT,
        filters={"service_id": f"eq.{TARGET['service_id']}"}, limit=10,
    )
    offer_rows = client.fetch_rows(
        TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_SELECT,
        filters={"id": f"eq.{TARGET['offer_id']}"}, limit=1,
    )
    if (
        not service_rows
        or not offer_rows
        or len(item_rows) != 1
        or not service_matches(service_rows[0])
        or not item_matches(item_rows[0])
        or not offer_matches(offer_rows[0])
    ):
        result["status"] = "precondition_failed"
        return result
    result["status"] = "ready"
    if not apply:
        return result
    detached = client.update_row(
        TABLE_PROMO_OFFER_ITEMS,
        {
            "offer_item_id": f"eq.{TARGET['offer_item_id']}",
            "offer_id": f"eq.{TARGET['offer_id']}",
            "service_id": f"eq.{TARGET['service_id']}",
        },
        {"service_id": None, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if not detached:
        result["status"] = "detach_race"
        return result
    deleted = audit_then_delete_rows(
        client,
        TABLE_CLINIC_SERVICES,
        {
            "service_id": f"eq.{TARGET['service_id']}",
            "business_id": f"eq.{TARGET['business_id']}",
            "source_url": f"eq.{TARGET['source_url']}",
            "regular_price": "is.null",
        },
        applied_by=f"agent:{script_agent_name()}",
        reason="soft_delete: unsupported_catalog_service_link",
        target_offer_id=int(TARGET["offer_id"]),
    )
    result["applied"] = bool(deleted)
    if not deleted:
        result["status"] = "service_delete_failed_after_detach"
    return result


def main() -> int:
    args = parse_args()
    result = run(load_client(), apply=args.apply)
    args.result_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.result_dir / f"unsupported_catalog_service_link_repair_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    result_path.write_text(
        json.dumps({"dry_run": not args.apply, "result": result}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"result_path": str(result_path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
