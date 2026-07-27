#!/usr/bin/env python3
"""Normalize promo offer totals and item unit prices with a reviewable dry-run."""
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

from utils.offer_item_price_repair import build_offer_item_price_plan  # noqa: E402
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402

TABLE_MASTER = "promo_offer_master"
TABLE_ITEMS = "promo_offer_items"
SELECT = "id,regular_price,discount_price,price_model,updated_at,promo_offer_items(offer_item_id,quantity,unit_price,updated_at,service_id)"
PAGE_SIZE = 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="执行计划；默认只生成 dry-run JSON")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "output" / "results"))
    return parser.parse_args()


def load_reader() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("缺少 SUPABASE_URL 或只读 publishable/anon key")
    return SupabaseRestClient(url, key)


def fetch_all_rows(client: SupabaseRestClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(TABLE_MASTER, SELECT, limit=PAGE_SIZE, offset=offset, order="id.asc")
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def main() -> None:
    args = parse_args()
    plan = build_offer_item_price_plan(fetch_all_rows(load_reader()))
    applied: dict[str, list[dict[str, Any]]] = {"master": [], "items": []}
    errors: list[str] = []
    if args.apply:
        load_dotenv(PROJECT_ROOT / ".env")
        writer = SupabaseRestClient(os.environ["SUPABASE_URL"], get_supabase_writer_key())
        master_timestamps = {row["id"]: row.get("updated_at") for row in fetch_all_rows(load_reader())}
        item_timestamps = {
            item["offer_item_id"]: item.get("updated_at")
            for row in fetch_all_rows(load_reader())
            for item in row.get("promo_offer_items", [])
            if item.get("offer_item_id") is not None
        }
        for update in plan.master_updates:
            payload = {key: value for key, value in update.items() if key != "id"}
            timestamp = master_timestamps.get(update["id"])
            if not timestamp:
                errors.append(f"master {update['id']} missing updated_at precondition")
                continue
            updated = writer.update_row(TABLE_MASTER, {"id": f"eq.{update['id']}", "updated_at": f"eq.{timestamp}"}, payload)
            if not updated:
                errors.append(f"master {update['id']} changed since dry-run")
                continue
            applied["master"].append({"id": update["id"], "updated": updated})
        for update in plan.updates:
            payload = {"unit_price": update["unit_price"]}
            item_id = update["offer_item_id"]
            timestamp = item_timestamps.get(item_id)
            if not timestamp:
                errors.append(f"item {item_id} missing updated_at precondition")
                continue
            updated = writer.update_row(TABLE_ITEMS, {"offer_item_id": f"eq.{item_id}", "updated_at": f"eq.{timestamp}"}, payload)
            if not updated:
                errors.append(f"item {item_id} changed since dry-run")
                continue
            actual_value = updated[0].get("unit_price")
            if actual_value != update["unit_price"]:
                errors.append(f"item {item_id} did not retain requested unit_price")
                continue
            applied["items"].append({"offer_item_id": item_id, "updated": updated})

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"promo_offer_item_price_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_path.write_text(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "master_updates": plan.master_updates,
                "item_updates": plan.updates,
                "skipped": plan.skipped,
                "applied": applied,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "master_updates": len(plan.master_updates), "item_updates": len(plan.updates), "skipped": len(plan.skipped), "errors": len(errors), "output": str(output_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
