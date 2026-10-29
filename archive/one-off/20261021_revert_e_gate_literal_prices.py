#!/usr/bin/env python3
"""Restore computed promo prices after temporary E-gate literal workaround."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.promo_offer_items_db import upsert_offer_items
from utils.schema_contract import TABLE_PROMO_OFFER_MASTER
from utils.supabase_access import load_supabase_client

# Pre–literal-workaround values (computed discount totals / unit economics).
RESTORE: dict[int, dict] = {
    7122: {"discount_price": 750.0, "discount_percent": 9.09, "discount_amount": 75.0, "regular_price": 825.0, "price_model": "from"},
    8509: {"discount_price": 400.0, "discount_percent": 11.11, "discount_amount": 50.0, "regular_price": 450.0, "price_model": "total"},
    8800: {"discount_price": 405.0, "discount_percent": 10.0, "discount_amount": 45.0, "regular_price": 450.0, "price_model": "total"},
    9338: {"discount_price": 550.0, "discount_percent": 8.33, "discount_amount": 50.0, "regular_price": 600.0, "price_model": "per_unit"},
    9346: {"discount_price": 500.0, "discount_percent": 16.67, "discount_amount": 100.0, "regular_price": 600.0, "price_model": "per_unit"},
    10059: {"discount_price": 382.5, "discount_percent": 15.0, "discount_amount": 67.5, "regular_price": 450.0, "price_model": "total"},
    10823: {"discount_price": 520.0, "discount_percent": 33.33, "discount_amount": 260.0, "regular_price": 780.0, "price_model": "total"},
}

ITEM_RESTORE: dict[int, list[dict]] = {
    8800: [{"item_name": "Skinvive", "quantity": None, "unit_price": 405.0}],
    10059: [{"item_name": "Skinvive", "quantity": None, "unit_price": 382.5}],
    10823: [{"item_name": "Botox", "quantity": 60, "unit_price": 8.6667}],
}


def main() -> None:
    client = load_supabase_client(ROOT)
    client.assert_write_project()
    now = datetime.now(timezone.utc).isoformat()
    for offer_id, patch in RESTORE.items():
        rows = client.fetch_rows(TABLE_PROMO_OFFER_MASTER, "id,updated_at", filters={"id": f"eq.{offer_id}"}, limit=1)
        if not rows:
            continue
        client.update_row(
            TABLE_PROMO_OFFER_MASTER,
            {"id": f"eq.{offer_id}", "updated_at": f"eq.{rows[0]['updated_at']}"},
            {**patch, "updated_at": now},
        )
    for offer_id, items in ITEM_RESTORE.items():
        upsert_offer_items(client, offer_id, items)
    from utils.batch_gate_checks import run_batch_gate

    gate = run_batch_gate(ROOT)
    print({"gate_passed": gate["passed"], "E": len(gate["checks"]["E_price_not_in_text"]["failing"])})
    if not gate["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
