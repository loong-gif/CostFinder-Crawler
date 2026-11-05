#!/usr/bin/env python3
"""Remove only currently provable cross-business promo_offer_items links.

The command defaults to a no-write plan. On ``--apply`` it rereads every item,
offer, and service immediately before deleting the link, so it is safe to rerun
and cannot delete a link that has since been corrected.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="execute guarded deletes (default: dry-run)")
    parser.add_argument("--result-dir", type=Path, default=PROJECT_ROOT / "output" / "results")
    return parser.parse_args()


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = get_supabase_writer_key()
    if not base_url or not key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_WRITER_KEY")
    return SupabaseRestClient(base_url, key)


def plan_repairs(
    items: Iterable[Mapping[str, Any]],
    offers: Iterable[Mapping[str, Any]],
    services: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Return delete actions only where both sides have conflicting businesses."""
    offer_by_id = {row.get("id"): row for row in offers if row.get("id") is not None}
    service_by_id = {row.get("service_id"): row for row in services if row.get("service_id") is not None}
    actions: List[Dict[str, Any]] = []
    for item in items:
        item_id = item.get("offer_item_id")
        offer_id = item.get("offer_id")
        service_id = item.get("service_id")
        offer = offer_by_id.get(offer_id) or {}
        service = service_by_id.get(service_id) or {}
        offer_business_id = offer.get("business_id")
        service_business_id = service.get("business_id")
        if (
            item_id is None
            or offer_id is None
            or service_id is None
            or offer_business_id is None
            or service_business_id is None
            or offer_business_id == service_business_id
        ):
            continue
        actions.append(
            {
                "action": "delete_cross_business_item",
                "offer_item_id": item_id,
                "expected_offer_id": offer_id,
                "expected_service_id": service_id,
                "expected_offer_business_id": offer_business_id,
                "expected_service_business_id": service_business_id,
                "service_name": service.get("service_name"),
                "offer_raw_text": offer.get("offer_raw_text"),
                "status": "ready",
            }
        )
    return actions


def _fetch_one(client: Any, table: str, select: str, filters: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    rows = client.fetch_rows(table, select, filters=dict(filters), limit=1)
    return dict(rows[0]) if rows else None


def apply_repairs(client: Any, actions: Sequence[Mapping[str, Any]], *, apply: bool) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for action in actions:
        result = {"applied": False, **dict(action)}
        if not apply:
            results.append(result)
            continue
        item = _fetch_one(
            client,
            TABLE_PROMO_OFFER_ITEMS,
            OFFER_ITEM_SELECT,
            {"offer_item_id": f"eq.{action['offer_item_id']}"},
        )
        if not item or item.get("offer_id") != action["expected_offer_id"] or item.get("service_id") != action["expected_service_id"]:
            result["status"] = "precondition_failed"
            results.append(result)
            continue
        offer = _fetch_one(
            client,
            TABLE_PROMO_OFFER_MASTER,
            OFFER_MASTER_SELECT,
            {"id": f"eq.{action['expected_offer_id']}"},
        )
        service = _fetch_one(
            client,
            TABLE_CLINIC_SERVICES,
            CLINIC_SERVICE_SELECT,
            {"service_id": f"eq.{action['expected_service_id']}"},
        )
        if (
            not offer
            or not service
            or offer.get("business_id") != action["expected_offer_business_id"]
            or service.get("business_id") != action["expected_service_business_id"]
            or offer.get("business_id") == service.get("business_id")
        ):
            result["status"] = "precondition_failed"
            results.append(result)
            continue
        deleted = audit_then_delete_rows(
            client,
            TABLE_PROMO_OFFER_ITEMS,
            {
                "offer_item_id": f"eq.{action['offer_item_id']}",
                "offer_id": f"eq.{action['expected_offer_id']}",
                "service_id": f"eq.{action['expected_service_id']}",
            },
            applied_by=f"agent:{script_agent_name()}",
            reason="soft_delete: cross_business_offer_item",
            target_offer_id=int(action["expected_offer_id"]),
        )
        result["deleted_row"] = deleted[0] if deleted else None
        result["applied"] = bool(deleted)
        if not deleted:
            result["status"] = "precondition_race"
        results.append(result)
    return results


def fetch_all(client: Any, table: str, select: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(table, select, limit=1000, offset=offset)
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        offset += len(batch)


def main() -> int:
    args = parse_args()
    client = load_client()
    actions = plan_repairs(
        fetch_all(client, TABLE_PROMO_OFFER_ITEMS, OFFER_ITEM_SELECT),
        fetch_all(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_SELECT),
        fetch_all(client, TABLE_CLINIC_SERVICES, CLINIC_SERVICE_SELECT),
    )
    results = apply_repairs(client, actions, apply=args.apply)
    args.result_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.result_dir / f"cross_business_offer_item_repair_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    result_path.write_text(
        json.dumps({"dry_run": not args.apply, "actions": actions, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"result_path": str(result_path), "planned": len(actions), "applied": sum(bool(row["applied"]) for row in results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
