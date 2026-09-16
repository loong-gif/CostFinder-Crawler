#!/usr/bin/env python3
"""Delete only orphaned catalog services with no verifiable catalog evidence.

Eligible rows must simultaneously have an ineligible source URL, no catalog
price, and no promo_offer_items references.  The command is dry-run by default
and rereads every candidate immediately before a guarded delete.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.agent_defense import audit_then_delete_rows, script_agent_name  # noqa: E402
from utils.schema_contract import (  # noqa: E402
    CLINIC_SERVICE_SELECT,
    OFFER_ITEM_SELECT,
    TABLE_CLINIC_SERVICES,
    TABLE_PROMO_OFFER_ITEMS,
)
from utils.service_price_guard import is_catalog_ineligible_url  # noqa: E402
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402

CONFIRMED_FOREIGN_SOCIAL_SERVICES = {
    2926: {
        "business_id": 1908,
        "service_name": "Dermal Filler",
        "regular_price": 700.0,
        "source_url": "https://www.facebook.com/Mariposamedspa/posts/pfbid0HyCXNwMYp91Tmf4JUjErXszxreQoeYse9Vgzm2JVvBhcgJS2a7NfREfhV782MTBil",
    },
    2943: {
        "business_id": 1908,
        "service_name": "Botox",
        "regular_price": 15.0,
        "source_url": "https://www.facebook.com/bespokeskinboutique/posts/pfbid02S619BR7oarN62SdBDnzdseEg1YGUeXqr5LA1sTdD5vtsUpk4ZXvtqk864hXepDVDl",
    },
}

CONFIRMED_UNLINKED_UNVERIFIED_SERVICES = {
    2909: {
        "business_id": 2191,
        "service_name": "Dermal Filler",
        "regular_price": 700.0,
        "source_url": "https://www.themedspotla.com/membership",
    },
}


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
    services: Iterable[Mapping[str, Any]], items: Iterable[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    referenced_service_ids = {
        row.get("service_id") for row in items if row.get("service_id") is not None
    }
    actions: List[Dict[str, Any]] = []
    for service in services:
        service_id = service.get("service_id")
        source_url = str(service.get("source_url") or "").strip()
        confirmed = (
            CONFIRMED_FOREIGN_SOCIAL_SERVICES.get(service_id)
            or CONFIRMED_UNLINKED_UNVERIFIED_SERVICES.get(service_id)
        )
        if confirmed and all(service.get(key) == value for key, value in confirmed.items()):
            if service_id not in referenced_service_ids:
                actions.append(
                    {
                        "action": "delete_unlinked_unverified_service",
                        "service_id": service_id,
                        "expected_business_id": service.get("business_id"),
                        "expected_source_url": source_url,
                        "expected_regular_price": service.get("regular_price"),
                        "service_name": service.get("service_name"),
                        "status": "ready",
                    }
                )
            continue
        if (
            service_id is None
            or service_id in referenced_service_ids
            or service.get("regular_price") is not None
            or not source_url
            or not is_catalog_ineligible_url(source_url)
        ):
            continue
        actions.append(
            {
                "action": "delete_unlinked_ineligible_service",
                "service_id": service_id,
                "expected_business_id": service.get("business_id"),
                "expected_source_url": source_url,
                "expected_regular_price": None,
                "service_name": service.get("service_name"),
                "status": "ready",
            }
        )
    return actions


def _is_current_candidate(client: Any, action: Mapping[str, Any]) -> bool:
    rows = client.fetch_rows(
        TABLE_CLINIC_SERVICES,
        CLINIC_SERVICE_SELECT,
        filters={"service_id": f"eq.{action['service_id']}"},
        limit=1,
    )
    if not rows:
        return False
    service = rows[0]
    if (
        service.get("business_id") != action["expected_business_id"]
        or str(service.get("source_url") or "").strip() != action["expected_source_url"]
        or service.get("regular_price") != action.get("expected_regular_price")
        or not is_catalog_ineligible_url(str(service.get("source_url") or ""))
    ):
        return False
    references = client.fetch_rows(
        TABLE_PROMO_OFFER_ITEMS,
        OFFER_ITEM_SELECT,
        filters={"service_id": f"eq.{action['service_id']}"},
        limit=1,
    )
    return not references


def apply_repairs(client: Any, actions: Sequence[Mapping[str, Any]], *, apply: bool) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for action in actions:
        result = {"applied": False, **dict(action)}
        if not apply:
            results.append(result)
            continue
        if not _is_current_candidate(client, action):
            result["status"] = "precondition_failed"
            results.append(result)
            continue
        deleted = audit_then_delete_rows(
            client,
            TABLE_CLINIC_SERVICES,
            {
                "service_id": f"eq.{action['service_id']}",
                "business_id": f"eq.{action['expected_business_id']}",
                "source_url": f"eq.{action['expected_source_url']}",
                "regular_price": "is.null" if action.get("expected_regular_price") is None else f"eq.{action['expected_regular_price']}",
            },
            applied_by=f"agent:{script_agent_name()}",
            reason="soft_delete: unlinked_ineligible_service",
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
        fetch_all(client, TABLE_CLINIC_SERVICES, CLINIC_SERVICE_SELECT),
        fetch_all(client, TABLE_PROMO_OFFER_ITEMS, OFFER_ITEM_SELECT),
    )
    results = apply_repairs(client, actions, apply=args.apply)
    args.result_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.result_dir / f"unlinked_ineligible_service_repair_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    result_path.write_text(
        json.dumps({"dry_run": not args.apply, "actions": actions, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"result_path": str(result_path), "planned": len(actions), "applied": sum(bool(row["applied"]) for row in results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
