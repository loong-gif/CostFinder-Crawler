#!/usr/bin/env python3
"""Create or apply a reviewable promo-offer price repair plan.

The default is read-only. ``--apply`` uses optimistic ``updated_at`` predicates
and writes unresolved errors to the protected review queue.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_promo_offer_prices import fetch_all  # noqa: E402
from utils.promo_offer_price_quality import build_price_repair_plan  # noqa: E402
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="应用带 updated_at 前置条件的修复")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "output" / "results"))
    return parser.parse_args()


def _client(*, writer: bool) -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_URL")
    key = get_supabase_writer_key() if writer else (os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY"))
    if not url or not key:
        raise RuntimeError("缺少 SUPABASE_URL 或所需凭据")
    return SupabaseRestClient(url, key)


def _apply_plan(client: SupabaseRestClient, plan) -> dict[str, Any]:
    applied = {"master": [], "items": [], "review_cases": 0, "conflicts": []}
    for update in plan.master_updates:
        payload = {key: value for key, value in update.items() if key not in {"id", "updated_at"}}
        updated = client.update_row(
            "promo_offer_master",
            {"id": f"eq.{update['id']}", "updated_at": f"eq.{update['updated_at']}"},
            payload,
        )
        if updated:
            applied["master"].append(update["id"])
        else:
            applied["conflicts"].append({"table": "promo_offer_master", "id": update["id"]})
    for update in plan.item_updates:
        payload = {"unit_price": update["unit_price"]}
        updated = client.update_row(
            "promo_offer_items",
            {"offer_item_id": f"eq.{update['offer_item_id']}", "updated_at": f"eq.{update['updated_at']}"},
            payload,
        )
        if updated:
            applied["items"].append(update["offer_item_id"])
        else:
            applied["conflicts"].append({"table": "promo_offer_items", "id": update["offer_item_id"]})
    cases = [case.as_row() for case in plan.review_cases]
    if cases:
        client.upsert_rows("promo_offer_quality_reviews", cases, on_conflict="offer_id,rule_name")
        applied["review_cases"] = len(cases)
    return applied


def main() -> int:
    args = parse_args()
    rows = fetch_all(_client(writer=False))
    plan = build_price_repair_plan(rows)
    applied: dict[str, Any] = {}
    if args.apply:
        applied = _apply_plan(_client(writer=True), plan)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"promo_offer_price_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    payload = {
        "mode": "apply" if args.apply else "dry_run",
        "master_updates": plan.master_updates,
        "item_updates": plan.item_updates,
        "review_cases": [case.as_row() for case in plan.review_cases],
        "applied": applied,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(path), "master_updates": len(plan.master_updates), "item_updates": len(plan.item_updates), "review_cases": len(plan.review_cases), "mode": payload["mode"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
