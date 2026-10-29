#!/usr/bin/env python3
"""Repair 2026-10-21 QA rejections that the current page still proves.

Naked TOX member offers keep the labeled compare-at as regular_price and gain a
non-member sibling for the sale that used to be stored in that column.
Promotion 216's IPL and Forma gifts are reactivated. Promotion 177's per-service
New Patient / New to You rows are deactivated, replaced by the current first-visit
20% wording. Offer 8016 is deactivated because DermFX October specials no longer
list that September Lip Flip / Brow Lift price.

Dry-run unless --apply. A row is skipped when its id or updated_at moved.
"""
from __future__ import annotations

import json
import re
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.offer_fingerprint import compute_offer_fingerprint
from utils.supabase_access import load_supabase_client

TOX_IDS = [10468, 11071, 11072, 11073, 11074, 11127, 11128, 11129, 11130, 11131]
RELIVE_IDS = {10507: "IPL", 10508: "Forma"}
ACE_IDS = list(range(10141, 10149))
DERMFX_ID = 8016
COMPARE_RE = re.compile(r"(?:regular(?:\s+price)?|compare)\D{0,20}(\d[\d,]*(?:\.\d+)?)", re.I)
NONMEMBER_RE = re.compile(r"non[- ]?member\D{0,40}(\d[\d,]*(?:\.\d+)?)", re.I)
UNITS_RE = re.compile(r"(\d+)\s+units", re.I)
DROP = {"id", "created_at", "updated_at"}


def money(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def percent(regular, discount) -> float:
    gap = Decimal(str(regular)) - Decimal(str(discount))
    return float((gap / Decimal(str(regular)) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def amount(regular, discount) -> float:
    return money(Decimal(str(regular)) - Decimal(str(discount)))


def near(left, right) -> bool:
    return left is not None and right is not None and abs(float(left) - float(right)) <= 0.02


def numbered(text, pattern):
    match = pattern.search(text or "")
    return money(match.group(1).replace(",", "")) if match else None


def ids(values) -> str:
    return f"in.({','.join(str(v) for v in values)})"


def units_of(offer, items) -> int | None:
    counts = [item.get("delivered_units") for item in items if item.get("delivered_units")]
    if len(counts) == 1:
        return int(counts[0])
    match = UNITS_RE.search(offer.get("offer_raw_text") or "")
    return int(match.group(1)) if match else None


def main(apply: bool) -> None:
    client = load_supabase_client(ROOT, role="writer")
    client.assert_write_project()
    tox = client.fetch_rows("promo_offer_master", "*", filters={"id": ids(TOX_IDS)})
    items = client.fetch_rows("promo_offer_items", "*", filters={"offer_id": ids(TOX_IDS)})
    by_offer = {}
    for item in items:
        by_offer.setdefault(item["offer_id"], []).append(item)
    peers = client.fetch_rows(
        "promo_offer_master", "id,promotion_id,discount_price,is_membership_required,is_active,offer_raw_text",
        filters={"promotion_id": "in.(58,920)", "is_active": "eq.true"})
    relive = client.fetch_rows("promo_offer_master", "id,promotion_id,is_active,offer_raw_text,updated_at",
                                filters={"promotion_id": "eq.216"})
    ace = client.fetch_rows("promo_offer_master", "id,is_active,offer_raw_text,updated_at,discount_percent,is_new_customer_required",
                             filters={"promotion_id": "eq.177"})
    derm = client.fetch_rows("promo_offer_master", "id,is_active,offer_raw_text,updated_at", filters={"id": "eq.8016"})
    parents = client.fetch_rows("clinic_promotions", "promotion_id,is_active,source_url",
                                 filters={"promotion_id": "in.(58,177,186,216,920)"})
    parent_active = {row["promotion_id"]: row["is_active"] for row in parents}
    plan = {"price_updates": [], "siblings": [], "reactivate": [], "deactivate": [], "ace_inserts": [], "review": []}

    for offer in sorted(tox, key=lambda row: row["id"]):
        raw = offer.get("offer_raw_text") or ""
        compare = numbered(raw, COMPARE_RE)
        sale = numbered(raw, NONMEMBER_RE)
        if sale is None and compare is not None and not near(offer.get("regular_price"), compare):
            sale = money(offer["regular_price"]) if offer.get("regular_price") is not None else None
        pack = units_of(offer, by_offer.get(offer["id"], []))
        if not offer.get("is_active") or not offer.get("is_membership_required") or compare is None or pack is None:
            plan["review"].append({"id": offer["id"], "reason": "tox_row_not_ready", "compare": compare, "units": pack})
            continue
        if not near(offer.get("discount_price"), None) and float(offer["discount_price"]) >= compare:
            plan["review"].append({"id": offer["id"], "reason": "member_sale_not_below_compare"})
            continue
        if not near(offer.get("regular_price"), compare):
            plan["price_updates"].append({
                "id": offer["id"], "updated_at": offer["updated_at"], "from": offer.get("regular_price"),
                "regular_price": compare, "discount_amount": amount(compare, offer["discount_price"]),
                "discount_percent": percent(compare, offer["discount_price"]),
            })
        if sale is None or not (float(offer["discount_price"]) < sale < compare):
            plan["review"].append({"id": offer["id"], "reason": "non_member_sale_unproven", "sale": sale})
            continue
        exists = any(peer["promotion_id"] == offer["promotion_id"] and not peer.get("is_membership_required")
                     and near(peer.get("discount_price"), sale)
                     and re.search(rf"non[- ]?member\D{{0,24}}{pack}\s+units", peer.get("offer_raw_text") or "", re.I)
                     for peer in peers)
        if exists:
            continue
        text = f"Naked TOX | Neurotoxins: Non-member {pack} units ${sale:.2f} (regular ${compare:.2f})"
        plan["siblings"].append({"source_id": offer["id"], "promotion_id": offer["promotion_id"],
                                  "updated_at": offer["updated_at"], "sale": sale, "compare": compare,
                                  "units": pack, "offer_raw_text": text})

    active_relive = [row for row in relive if row["is_active"] and row["id"] not in RELIVE_IDS]
    if not parent_active.get(216):
        plan["review"].append({"id": 216, "reason": "promotion_inactive"})
    elif any("IPL" in (row.get("offer_raw_text") or "") and "Forma" in (row.get("offer_raw_text") or "") for row in active_relive):
        plan["review"].append({"id": 216, "reason": "combined_active_offer_already_present"})
    else:
        for row in relive:
            gift = RELIVE_IDS.get(row["id"])
            if gift and not row["is_active"] and gift in (row.get("offer_raw_text") or ""):
                plan["reactivate"].append({"id": row["id"], "updated_at": row["updated_at"], "gift": gift})

    ace_blob = " ".join((row.get("offer_raw_text") or "") for row in ace if row["is_active"] and row["id"] not in ACE_IDS)
    for row in ace:
        if row["id"] in ACE_IDS and row["is_active"] and re.search(r"New (patient|to You)", row.get("offer_raw_text") or "", re.I):
            plan["deactivate"].append({"id": row["id"], "updated_at": row["updated_at"], "reason": "ace_page_no_longer_lists_this_split"})
    if parent_active.get(177) and "first treatment" not in ace_blob.lower():
        plan["ace_inserts"].append("New patients enjoy 20% off their first treatment. Excludes weight-loss services and peptides. One redemption per patient. Not valid with other offers.")
    if parent_active.get(177) and "skincare" not in ace_blob.lower():
        plan["ace_inserts"].append("New patients enjoy 20% off their first medical-grade skincare purchase. One redemption per patient. Not valid with other offers.")

    for row in derm:
        if row["is_active"] and re.search(r"Lip Flip or Brow Lift", row.get("offer_raw_text") or ""):
            plan["deactivate"].append({"id": row["id"], "updated_at": row["updated_at"], "reason": "dermfx_october_specials_replaced_september"})

    print(json.dumps({"apply": apply, "counts": {key: len(value) for key, value in plan.items()}, "plan": plan}, indent=1, default=str))
    if not apply:
        return

    tox_by_id = {row["id"]: row for row in tox}
    for update in plan["price_updates"]:
        current = client.fetch_rows("promo_offer_master", "id,updated_at,regular_price", filters={"id": f"eq.{update['id']}"}, limit=1)
        if not current or current[0]["updated_at"] != update["updated_at"] or near(current[0].get("regular_price"), update["regular_price"]):
            plan.setdefault("conflicts", []).append(update["id"])
            continue
        client.update_row("promo_offer_master", {"id": f"eq.{update['id']}", "updated_at": f"eq.{update['updated_at']}"}, {
            "regular_price": update["regular_price"], "discount_amount": update["discount_amount"],
            "discount_percent": update["discount_percent"]})
    for sibling in plan["siblings"]:
        source = tox_by_id[sibling["source_id"]]
        fresh = client.fetch_rows("promo_offer_master", "*", filters={"id": f"eq.{source['id']}", "is_active": "eq.true"}, limit=1)
        if not fresh or not near(fresh[0].get("regular_price"), sibling["compare"]):
            plan.setdefault("conflicts", []).append({"source_id": sibling["source_id"], "reason": "compare_not_stored"})
            continue
        row = {key: value for key, value in fresh[0].items() if key not in DROP}
        row.update(is_membership_required=False, is_new_customer_required=False, regular_price=sibling["compare"],
                   discount_price=sibling["sale"], discount_amount=amount(sibling["compare"], sibling["sale"]),
                   discount_percent=percent(sibling["compare"], sibling["sale"]), offer_raw_text=sibling["offer_raw_text"])
        conditions = row.get("conditions") or []
        if isinstance(conditions, list):
            row["conditions"] = ["Non-Member" if str(part).strip().lower() == "member" else part for part in conditions]
        row["offer_fingerprint"] = compute_offer_fingerprint(
            source_url="", service_name="", offer_raw_text=sibling["offer_raw_text"],
            business_id=row.get("business_id"), promotion_id=row.get("promotion_id"),
            membership_plan_id=row.get("membership_plan_id"))
        inserted = client.insert_rows("promo_offer_master", [row])
        new_id = inserted[0]["id"]
        copies = []
        for item in by_offer.get(source["id"], []):
            copy = {key: value for key, value in item.items() if key not in {"offer_item_id", "created_at", "updated_at"}}
            copy["offer_id"] = new_id
            name = str(copy.get("item_name") or "")
            copy["item_name"] = re.sub(r"\bMember\b", "Non-Member", name) if re.search(r"\bMember\b", name) else name
            copies.append(copy)
        if copies:
            client.insert_rows("promo_offer_items", copies)
        # Activation runs after items exist; the insert trigger closes an offer that has none yet.
        client.update_row("promo_offer_master", {"id": f"eq.{new_id}", "is_active": "eq.false"}, {"is_active": True})
        sibling["new_id"] = new_id
    for update in plan["reactivate"]:
        client.update_row("promo_offer_master", {"id": f"eq.{update['id']}", "updated_at": f"eq.{update['updated_at']}", "is_active": "eq.false"},
                          {"is_active": True})
    for update in plan["deactivate"]:
        client.update_row("promo_offer_master", {"id": f"eq.{update['id']}", "updated_at": f"eq.{update['updated_at']}", "is_active": "eq.true"},
                          {"is_active": False})
    ace_row = next((row for row in ace if row["id"] in ACE_IDS), None)
    if ace_row and plan["ace_inserts"]:
        template = client.fetch_rows("promo_offer_master", "*", filters={"id": f"eq.{ace_row['id']}"}, limit=1)[0]
        for text in plan["ace_inserts"]:
            row = {key: value for key, value in template.items() if key not in DROP}
            row.update(is_active=True, is_membership_required=False, is_new_customer_required=True,
                       regular_price=None, discount_price=None, discount_amount=None, discount_percent=20,
                       price_model="total", offer_raw_text=text, conditions=[])
            row["offer_fingerprint"] = compute_offer_fingerprint(
                source_url="", service_name="", offer_raw_text=text, business_id=row.get("business_id"),
                promotion_id=177, membership_plan_id=row.get("membership_plan_id"))
            inserted = client.insert_rows("promo_offer_master", [row])
            label = "first treatment" if "first treatment" in text else "medical-grade skincare"
            client.insert_rows("promo_offer_items", [{
                "offer_id": inserted[0]["id"], "item_name": label, "service_id": None,
                "delivered_units": None, "unit_price": None}])
            client.update_row("promo_offer_master", {"id": f"eq.{inserted[0]['id']}", "is_active": "eq.false"}, {"is_active": True})
    print(json.dumps({"applied": True, "siblings": plan["siblings"], "conflicts": plan.get("conflicts", [])}, default=str))


if __name__ == "__main__":
    main("--apply" in sys.argv)
