"""Merge active new-patient / returning-patient duplicate offers (full library).

Rule: same promotion scope + same item composition → one offer. Keep the new-patient row;
returning-patient discount_price becomes survivor regular_price; deactivate returning siblings.

Dry-run by default. Apply: ALLOW_SERVICE_ROLE_WRITES=1 python one-off/20261021_merge_new_returning_offer_tiers.py --apply
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from utils.batch_gate_checks import linked_query, run_batch_gate
from utils.supabase_access import fetch_all_rows, load_supabase_client

OUT = ROOT / "output/merge_new_returning_20261021"
_NEW_TEXT = re.compile(r"\b(?:new|first[- ]?(?:time|visit|tox)|new patients?)\b", re.I)
_RETURNING_TEXT = re.compile(r"\b(?:returning|existing|regulars?|non[- ]?new)\b", re.I)
_NON_MEMBER_TEXT = re.compile(r"\bnon[- ]?members?\b", re.I)

FETCH_SQL = r"""
select m.id, m.business_id, m.promotion_id, m.membership_plan_id, m.is_package,
       m.is_membership_required, m.is_new_customer_required, m.conditions,
       m.discount_price, m.regular_price, m.price_model, m.offer_raw_text,
       coalesce(json_agg(json_build_object(
         'service_id', i.service_id,
         'item_name', i.item_name,
         'delivered_units', i.delivered_units
       ) order by i.service_id, i.offer_item_id) filter (where i.offer_item_id is not null), '[]'::json) as items
  from promo_offer_master m
  left join promo_offer_items i on i.offer_id = m.id
 group by m.id
"""


def ids(values: list[int]) -> str:
    return f"in.({','.join(map(str, values))})"


def _tier_side(row: dict) -> str | None:
    if row.get("is_new_customer_required") is True:
        return "new"
    text = str(row.get("offer_raw_text") or "")
    if _NEW_TEXT.search(text):
        return "new"
    if row.get("is_new_customer_required") is False or _RETURNING_TEXT.search(text):
        return "returning"
    return None


def _effective_price(row: dict, items: list[dict]) -> Decimal | None:
    for field in ("discount_price", "regular_price"):
        price = _num(row.get(field))
        if price is not None:
            return price
    if (row.get("price_model") or "total") == "per_unit" and items:
        return _num(items[0].get("unit_price"))
    return None


def _num(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _merge_group_key(row: dict, items: list[dict]) -> tuple:
    composition = tuple(sorted(
        ((i.get("service_id"), i.get("delivered_units")) for i in items),
        key=lambda part: (repr(part[0]), repr(part[1])),
    ))
    return (row.get("business_id"), row.get("promotion_id"), row.get("membership_plan_id"),
            row.get("is_package"), row.get("is_membership_required"), composition)


def plan_merges(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (actions, skipped) where each action has survivor_id, regular_price, deactivate_ids."""
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        items = row.get("items") or []
        if isinstance(items, str):
            items = json.loads(items)
        if not items:
            continue
        key = _merge_group_key(row, items)
        buckets[key].append({**row, "items": items})

    actions: list[dict] = []
    skipped: list[dict] = []
    for key, group in buckets.items():
        new_rows = [r for r in group if _tier_side(r) == "new"]
        ret_rows = [r for r in group if _tier_side(r) == "returning"]
        if new_rows:
            lane = bool(new_rows[0].get("is_membership_required"))
            new_rows = [r for r in new_rows if bool(r.get("is_membership_required")) == lane]
            ret_rows = [r for r in ret_rows if bool(r.get("is_membership_required")) == lane]
            survivor_text = str(new_rows[0].get("offer_raw_text") or "")
            if not _NON_MEMBER_TEXT.search(survivor_text):
                new_rows = [r for r in new_rows if not _NON_MEMBER_TEXT.search(str(r.get("offer_raw_text") or ""))]
                ret_rows = [r for r in ret_rows if not _NON_MEMBER_TEXT.search(str(r.get("offer_raw_text") or ""))]
        if not new_rows or not ret_rows:
            continue
        if len(new_rows) != 1:
            skipped.append({"reason": "ambiguous_new_tier", "offer_ids": [r["id"] for r in group],
                            "promotion_id": group[0].get("promotion_id")})
            continue
        survivor = min(new_rows, key=lambda r: (not bool(r.get("is_active")), int(r["id"])))
        new_price = _effective_price(survivor, survivor["items"])
        returning_candidates = []
        for row in ret_rows:
            price = _effective_price(row, row["items"])
            if price is not None and new_price is not None and price > new_price:
                returning_candidates.append((row, price))
        if len(returning_candidates) != 1:
            skipped.append({"reason": "ambiguous_returning_tier" if len(returning_candidates) > 1 else "missing_tier_price",
                            "survivor": survivor["id"],
                            "returning_ids": [r["id"] for r in ret_rows],
                            "promotion_id": group[0].get("promotion_id")})
            continue
        returning, ret_price = returning_candidates[0]
        existing_regular = _num(survivor.get("regular_price"))
        regular = float(ret_price)
        if existing_regular is not None and abs(existing_regular - ret_price) > Decimal("0.02"):
            skipped.append({"reason": "survivor_regular_conflicts", "survivor": survivor["id"],
                            "existing_regular": str(existing_regular), "returning_price": str(ret_price)})
            continue
        actions.append({
            "survivor_id": int(survivor["id"]),
            "returning_id": int(returning["id"]),
            "promotion_id": survivor.get("promotion_id"),
            "regular_price": regular,
            "patch_regular": existing_regular is None,
            "deactivate_ids": sorted({int(r["id"]) for r in ret_rows}),
        })
    return actions, skipped


def main(apply: bool) -> None:
    rows = linked_query(FETCH_SQL, root=ROOT)
    actions, skipped = plan_merges(rows)
    survivor_ids = [a["survivor_id"] for a in actions]
    deactivate_ids = [i for a in actions for i in a["deactivate_ids"]]
    touch_ids = sorted(set(survivor_ids + deactivate_ids))

    summary = {
        "offers_scanned": len(rows),
        "merge_groups": len(actions),
        "deactivate_offers": len(deactivate_ids),
        "skipped_groups": len(skipped),
        "sample_actions": actions[:15],
        "sample_skipped": skipped[:15],
    }
    print(json.dumps(summary, indent=2, default=str))

    if not apply:
        return
    if os.environ.get("ALLOW_SERVICE_ROLE_WRITES") != "1":
        raise SystemExit("set ALLOW_SERVICE_ROLE_WRITES=1")
    if not actions:
        print("nothing to apply")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    backup_path = OUT / "backup_before_merge.json"
    if backup_path.exists():
        raise SystemExit(f"{backup_path} exists; refuse to overwrite")

    client = load_supabase_client(role="service")
    backup = {
        "offers": fetch_all_rows(client, "promo_offer_master", "*", filters={"id": ids(touch_ids)}),
        "items": fetch_all_rows(client, "promo_offer_items", "*", filters={"offer_id": ids(touch_ids)}),
    }
    backup_path.write_text(json.dumps(backup, ensure_ascii=False, indent=1, default=str))

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    for action in actions:
        sid = action["survivor_id"]
        payload = {"is_new_customer_required": True, "updated_at": now}
        if action["patch_regular"]:
            payload["regular_price"] = action["regular_price"]
        updated += len(client.update_row("promo_offer_master", {"id": f"eq.{sid}"}, payload))
    deactivated = client.update_row(
        "promo_offer_master",
        {"id": ids(deactivate_ids)},
        {"is_active": False, "updated_at": now},
    )
    post = linked_query(FETCH_SQL, root=ROOT)
    _, skipped_after = plan_merges(post)
    gate = run_batch_gate(ROOT)
    report = {
        "updated_survivors": updated,
        "deactivated": len(deactivated),
        "remaining_skipped_groups": len(skipped_after),
        "batch_gate_passed": gate["passed"],
        "failing_checks": {k: len(v["failing"]) for k, v in gate["checks"].items() if v["failing"]},
    }
    (OUT / "apply_report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main("--apply" in sys.argv)
