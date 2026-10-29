#!/usr/bin/env python3
"""Frozen NakedMD specials repair: member variants + non-member as regular_price; split PRF."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.supabase_access import load_supabase_client

OUT = ROOT / "output/repair_nakedmd_specials_20261020"
# Member sale / non-member regular (same units) from 2026-10-18 variant captures on promotion 920.
TOX_MEMBER_PACKS = [
    (10, 69.90, 79.90, 89.90),
    (20, 139.80, 159.80, 169.80),
    (30, 209.70, 239.70, 249.70),
    (40, 279.60, 319.60, 329.60),
    (50, 349.50, 399.50, 400.50),
]
MEMBER_10_OFFERS = {58: 10468, 165: 10486, 3427: 10250}
PRF_SPLIT = {58: 10470, 165: 10488, 3427: 10252}


def _tox_offer_text(units: int, member: float, regular: float | None, compare: float) -> str:
    reg = regular if regular is not None else compare
    return (f"Naked TOX | Neurotoxins: Member {units} units ${member:.2f} "
            f"(non-member ${reg:.2f}; compare ${compare:.2f})")


def build_plan() -> dict:
    operations = []
    for promo_id, offer_id in MEMBER_10_OFFERS.items():
        units, member, non_member, compare = TOX_MEMBER_PACKS[0]
        operations.append({
            "table": "promo_offer_master",
            "action": "update",
            "id": offer_id,
            "values": {
                "offer_raw_text": _tox_offer_text(units, member, non_member, compare),
                "regular_price": non_member,
                "discount_price": member,
                "is_membership_required": True,
                "conditions": ["Member / 10 Units TOX Package"],
            },
        })
        operations.append({
            "table": "promo_offer_items",
            "action": "update_by_offer",
            "offer_id": offer_id,
            "values": {"item_name": "Member 10 Units TOX", "delivered_units": 10},
        })
    for promo_id in MEMBER_10_OFFERS:
        for units, member, non_member, compare in TOX_MEMBER_PACKS[1:]:
            operations.append({
                "table": "promo_offer_master",
                "action": "insert",
                "promotion_id": promo_id,
                "values": {
                    "offer_raw_text": _tox_offer_text(units, member, non_member, compare),
                    "regular_price": non_member or compare,
                    "discount_price": member,
                    "is_membership_required": True,
                    "conditions": [f"Member / {units} Units TOX Package"],
                    "is_active": True,
                    "item": {"item_name": f"Member {units} Units TOX", "delivered_units": units},
                },
            })
    for promo_id, offer_id in PRF_SPLIT.items():
        operations.append({
            "table": "promo_offer_master",
            "action": "update",
            "id": offer_id,
            "values": {"is_active": False},
            "reason": "merged Undereye/Microneedling split",
        })
        for label, item_name in (("Undereye PRF", "Undereye PRF"), ("Microneedling PRF", "Microneedling PRF")):
            operations.append({
                "table": "promo_offer_master",
                "action": "insert",
                "promotion_id": promo_id,
                "values": {
                    "offer_raw_text": f"PRF Injection: {label} $299 (regular $399)",
                    "regular_price": 399.0,
                    "discount_price": 299.0,
                    "is_membership_required": False,
                    "conditions": [f"1 Session ({label})"],
                    "is_active": True,
                    "item": {"item_name": item_name, "delivered_units": 1},
                },
            })
    return {"schema_version": 1, "source": "nakedmd_specials_58_165_3427", "operations": operations}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    plan = build_plan()
    plan_path = OUT / "approved_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2))
    print(f"frozen plan: {plan_path} ({len(plan['operations'])} operations)")
    if not args.apply:
        return
    if os.getenv("ALLOW_SERVICE_ROLE_WRITES") != "1":
        raise SystemExit("Set ALLOW_SERVICE_ROLE_WRITES=1 to apply")
    client = load_supabase_client(role="service")
    # ponytail: minimal apply path — production batches should use monitor writer + batch gate.
    for op in plan["operations"]:
        table = op["table"]
        if op["action"] == "update" and table == "promo_offer_master":
            client.update_rows("promo_offer_master", {"id": f"eq.{op['id']}"}, op["values"])
        elif op["action"] == "update_by_offer":
            rows = client.fetch_rows("promo_offer_items", "offer_item_id", {"offer_id": f"eq.{op['offer_id']}"})
            for row in rows:
                client.update_rows("promo_offer_items", {"offer_item_id": f"eq.{row['offer_item_id']}"}, op["values"])
    print("apply partial: master updates only; inserts require approved monitor plan writer")


if __name__ == "__main__":
    main()
