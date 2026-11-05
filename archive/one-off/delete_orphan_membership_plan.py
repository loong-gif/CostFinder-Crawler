#!/usr/bin/env python3
"""Delete one reviewed orphaned membership plan with unavailable source evidence."""
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
    CLINIC_MEMBERSHIP_SELECT,
    OFFER_MASTER_SELECT,
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402


TARGET = {
    "plan_id": 506,
    "business_id": 1372,
    "membership_name": "Inner Circle Membership",
    "membership_price": 89.0,
    "billing_period": "monthly",
    "minimum_commitment_months": None,
    "benefits": [],
    "source_url": "https://www.instagram.com/p/DXerFg3ja-y/",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="execute the guarded delete")
    parser.add_argument("--result-dir", type=Path, default=PROJECT_ROOT / "output" / "results")
    return parser.parse_args()


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = get_supabase_writer_key()
    if not base_url or not key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_WRITER_KEY")
    return SupabaseRestClient(base_url, key)


def current_row_matches(row: Mapping[str, Any], target: Mapping[str, Any] = TARGET) -> bool:
    return (
        row.get("plan_id") == target["plan_id"]
        and row.get("business_id") == target["business_id"]
        and str(row.get("membership_name") or "").strip() == target["membership_name"]
        and row.get("membership_price") is not None
        and float(row["membership_price"]) == target["membership_price"]
        and row.get("billing_period") == target["billing_period"]
        and row.get("minimum_commitment_months") is None
        and row.get("benefits") == target["benefits"]
        and str(row.get("source_url") or "").strip() == target["source_url"]
    )


def run(client: Any, *, apply: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {"plan_id": TARGET["plan_id"], "applied": False}
    rows = client.fetch_rows(
        TABLE_CLINIC_MEMBERSHIPS,
        CLINIC_MEMBERSHIP_SELECT,
        filters={"plan_id": f"eq.{TARGET['plan_id']}"},
        limit=1,
    )
    references = client.fetch_rows(
        TABLE_PROMO_OFFER_MASTER,
        OFFER_MASTER_SELECT,
        filters={"membership_plan_id": f"eq.{TARGET['plan_id']}"},
        limit=1,
    )
    if not rows or references or not current_row_matches(rows[0]):
        result["status"] = "precondition_failed"
        return result
    result["status"] = "ready"
    if not apply:
        return result
    deleted = audit_then_delete_rows(
        client,
        TABLE_CLINIC_MEMBERSHIPS,
        {
            "plan_id": f"eq.{TARGET['plan_id']}",
            "business_id": f"eq.{TARGET['business_id']}",
            "membership_price": f"eq.{TARGET['membership_price']}",
            "source_url": f"eq.{TARGET['source_url']}",
        },
        applied_by=f"agent:{script_agent_name()}",
        reason="soft_delete: orphan_membership_plan",
    )
    result["applied"] = bool(deleted)
    if not deleted:
        result["status"] = "precondition_race"
    return result


def main() -> int:
    args = parse_args()
    result = run(load_client(), apply=args.apply)
    args.result_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.result_dir / f"orphan_membership_plan_delete_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    result_path.write_text(
        json.dumps({"dry_run": not args.apply, "result": result}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"result_path": str(result_path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
