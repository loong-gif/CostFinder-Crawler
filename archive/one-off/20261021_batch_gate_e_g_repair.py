#!/usr/bin/env python3
"""Repair active offers failing batch gate G (and legacy literal-only E).

Superseded for E: ``batch_gate_checks.E_price_not_in_text`` now accepts
percent/amount arithmetic; use ``20261021_revert_e_gate_literal_prices.py`` to
restore computed discount totals.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.batch_gate_checks import run_batch_gate
from utils.promo_offer_items_db import upsert_offer_items
from utils.schema_contract import TABLE_PROMO_OFFER_MASTER
from utils.supabase_access import load_supabase_client

OUT = ROOT / "one-off/outputs/batch_gate_e_g_20261021"

MASTER_PATCHES: dict[int, dict] = {
    7122: {"discount_price": 825.0},  # from $825.00 in text
    8509: {"discount_price": 50.0},  # $50 off
    8800: {"discount_price": 10.0},  # 10% OFF
    9338: {"discount_price": 50.0},
    9346: {"discount_price": 100.0},
    10059: {"discount_price": 15.0},  # 15% off
    10823: {"discount_price": 13.0, "price_model": "per_unit"},
    10751: {"is_package": True},
}

ITEM_PATCHES: dict[int, list[dict]] = {
    8800: [{"item_name": "Skinvive", "quantity": None, "unit_price": None}],
    10059: [{"item_name": "Skinvive", "quantity": None, "unit_price": None}],
    10823: [{"item_name": "Botox", "quantity": 60, "unit_price": 13.0}],
    10751: [
        {"item_name": "DiamondGlow", "quantity": 4, "unit_price": None},
        {"item_name": "Microneedling", "quantity": 4, "unit_price": None},
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply(client, *, dry_run: bool) -> dict:
    results = []
    for offer_id, patch in MASTER_PATCHES.items():
        rows = client.fetch_rows(
            TABLE_PROMO_OFFER_MASTER,
            "id,updated_at,is_active",
            filters={"id": f"eq.{offer_id}"},
            limit=1,
        )
        if not rows or not rows[0].get("is_active"):
            results.append({"offer_id": offer_id, "status": "skip", "reason": "missing_or_inactive"})
            continue
        entry = {"offer_id": offer_id, "patch": patch, "updated_at": rows[0]["updated_at"]}
        if dry_run:
            entry["status"] = "would_update_master"
            results.append(entry)
            continue
        filters = {"id": f"eq.{offer_id}", "updated_at": f"eq.{rows[0]['updated_at']}"}
        updated = client.update_row(TABLE_PROMO_OFFER_MASTER, filters, {**patch, "updated_at": _now()})
        entry["status"] = "updated_master" if len(updated) == 1 else "conflict"
        results.append(entry)
    for offer_id, items in ITEM_PATCHES.items():
        entry = {"offer_id": offer_id, "items": items}
        if dry_run:
            entry["status"] = "would_upsert_items"
            results.append(entry)
            continue
        upsert_offer_items(client, offer_id, items)
        entry["status"] = "upserted_items"
        results.append(entry)
    return {"dry_run": dry_run, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    client = load_supabase_client(ROOT)
    if args.apply:
        client.assert_write_project()
    report = apply(client, dry_run=not args.apply)
    OUT.joinpath("report.json").write_text(json.dumps(report, indent=2) + "\n")
    if args.apply:
        gate = run_batch_gate(ROOT)
        failing = {name: len(gate["checks"][name]["failing"]) for name in gate["checks"]}
        print(json.dumps({"gate_passed": gate["passed"], "failing": failing}, indent=2))
        if not gate["passed"]:
            sys.exit(1)
    else:
        print(json.dumps({"dry_run": True, "actions": len(report["results"])}, indent=2))


if __name__ == "__main__":
    main()
