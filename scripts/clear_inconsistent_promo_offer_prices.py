#!/usr/bin/env python3
"""Clear promo_offer_master price fields contradicted by explicit source text.

Default mode is read-only and writes a reviewable JSON plan.  ``--apply`` only
clears individual contradictory fields; it never recalculates or fills values.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_price_evidence import extract_price_evidence, plan_inconsistent_price_field_clears  # noqa: E402
from utils.supabase_rest import SupabaseRestClient  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402

TABLE = "promo_offer_master"
FIELDS = "id,is_active,offer_raw_text,regular_price,discount_price,discount_amount,discount_percent"
PAGE_SIZE = 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="执行置空更新；默认仅生成计划")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "output" / "results"))
    return parser.parse_args()


def fetch_rows(client: SupabaseRestClient) -> List[Dict[str, Any]]:
    return fetch_all_rows(client, TABLE, FIELDS, page_size=PAGE_SIZE, order="id.asc")


def build_plan(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    plan: List[Dict[str, Any]] = []
    for row in rows:
        fields = plan_inconsistent_price_field_clears(row)
        remaining_discount = [
            row.get(field) if field not in fields else None
            for field in ("discount_price", "discount_amount", "discount_percent")
        ]
        if fields and row.get("is_active") and not any(
            value is not None and float(value) > 0 for value in remaining_discount
        ):
            # M016 requires active offers to retain one positive discount field.
            # Leave this record untouched for manual review rather than allowing
            # a partial update that the database will reject.
            continue
        if fields:
            plan.append(
                {
                    "id": row["id"],
                    "clear_fields": fields,
                    "stored": {field: row.get(field) for field in FIELDS.split(",") if field != "id"},
                    "text_evidence": extract_price_evidence(row.get("offer_raw_text")),
                }
            )
    return plan


def main() -> None:
    args = parse_args()
    reader = load_supabase_client(PROJECT_ROOT, role="reader")
    plan = build_plan(fetch_rows(reader))
    applied: List[Dict[str, Any]] = []
    if args.apply:
        writer = load_supabase_client(PROJECT_ROOT, role="writer")
        for item in plan:
            updated = writer.update_row(TABLE, item["id"], item["clear_fields"])
            applied.append({"id": item["id"], "updated": updated})

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"promo_offer_price_evidence_{timestamp}.json"
    output_path.write_text(
        json.dumps(
            {"mode": "apply" if args.apply else "dry_run", "candidate_count": len(plan), "plan": plan, "applied": applied},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "candidate_count": len(plan), "output": str(output_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
