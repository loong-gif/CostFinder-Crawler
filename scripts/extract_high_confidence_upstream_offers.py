#!/usr/bin/env python3
"""Extract source-backed offers from the reviewed high-confidence upstream set."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.offer_fingerprint import compute_offer_fingerprint  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402

PROMOTION_IDS = [14,45,161,186,198,199,207,209,212,216,217,232,613,626,671,684,687,692,693,778,811,831,848,854,855,856,865,871,873,886,887,899,902,905,928,990,3401,3402,3404,3407,3410,3414,3416,3419,3439,3441,3466,3482,3495,3497,3498,3499,3504,3514,3515,3517,3519,3520,3522,3523,3524,3525,3526,3527,3528,3530,3544,3545,3546,3547,3548,3550,3552]
MEMBERSHIP_IDS = [343,345,359,392,393,394,395,416,441,459,460,461,466,510,525,527,528,541,557,558,561,564,565,567,614,615,620,621,622,623,624,642,643,649,651,652,653,657,670,671]
TARGET_RE = re.compile(r"\b(botox|dysport|daxxify|xeomin|jeuveau|letybo|neurotoxin|filler|sculptra|juvederm|restylane|radiesse|kybella|skinvive)\b", re.I)
SIGNAL_RE = re.compile(r"(\$\s*[0-9]|[0-9]+\s*%\s*(?:off|discount)|\b(?:per|/)[ -]*(?:unit|syringe|vial)|\b(?:free|save|savings|vs\.?|regular|reg\.?|member price|members? price))", re.I)
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"


def source_lines(value: Any) -> list[str]:
    def split_line(item: str) -> list[str]:
        # Semicolon-separated specials are independently actionable in the source copy.
        return [part.strip(" -*•") for part in re.split(r"\s*;\s*", item) if part.strip(" -*•")]

    if isinstance(value, list):
        return [part for item in value if str(item).strip() for part in split_line(str(item).strip())]
    text = str(value or "").strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [part for item in parsed if str(item).strip() for part in split_line(str(item).strip())]
        except json.JSONDecodeError:
            pass
    return [part for line in re.split(r"\n+|(?<=\.)\s+(?=[A-Z])", text) if line.strip() for part in split_line(line)]


def money(value: str) -> float:
    return float(value.replace(",", ""))


def parse_line(line: str) -> dict[str, Any] | None:
    if not TARGET_RE.search(line) or not SIGNAL_RE.search(line):
        return None
    lower = line.lower()
    if "credit" in lower:
        return None
    service_match = TARGET_RE.search(line)
    service = service_match.group(1).lower() if service_match else ""
    model = "per_unit" if re.search(r"(?:per|/|a)\s*(?:unit|syringe|vial)|per\s+(?:session|treatment)", lower) else "total"
    if re.search(r"syringes?", lower) and re.search(r"\boff\b", lower):
        model = "per_unit"
    quantity = None
    quantity_match = re.search(r"\b(\d+|one|two|three|four|five)\s*(?:units?|syringes?|vials?|treatments?)\b", lower)
    if quantity_match:
        quantity_token = quantity_match.group(1)
        quantity = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}.get(quantity_token)
        if quantity is None:
            quantity = int(quantity_token)
    prices = [money(x) for x in re.findall(rf"\$\s*({NUMBER})", line)]
    if not prices:
        return None
    if "month" in lower and not re.search(r"(?:per|/|a)\s*(?:unit|syringe|vial)", lower):
        return None
    percent_match = re.search(rf"({NUMBER})\s*%\s*(?:off|discount)", lower)
    off_match = re.search(rf"\$\s*({NUMBER})\s*(?:off|discount)", lower)
    vs_match = re.search(rf"\$?\s*({NUMBER})\s*(?:vs\.?|versus)\s*\$?\s*({NUMBER})", lower)
    regular_match = re.search(rf"\b(?:regular(?:ly)?|reg\.?)\s*\$?\s*({NUMBER})", lower)
    if vs_match:
        discount_price, regular_price = money(vs_match.group(1)), money(vs_match.group(2))
        if regular_price <= 0 or discount_price > regular_price:
            return None
        amount = round(regular_price - discount_price, 2)
        percent = round(amount * 100 / regular_price, 2)
    elif regular_match and len(prices) >= 2:
        regular_price = money(regular_match.group(1))
        discount_price = prices[0] if prices[0] != regular_price else prices[1]
        if regular_price <= 0 or discount_price > regular_price:
            return None
        amount = round(regular_price - discount_price, 2)
        percent = round(amount * 100 / regular_price, 2)
    elif percent_match:
        percent = money(percent_match.group(1))
        discount_price = prices[0] if prices else None
        regular_price = None
        amount = None
    elif off_match:
        amount = money(off_match.group(1))
        percent = None
        discount_price = None
        regular_price = None
    elif re.search(r"\bfree\b", lower) and not re.search(r"\b(?:value|worth)\b", lower):
        discount_price, regular_price, amount, percent = 0, None, None, 100
    else:
        # A stated retail value is evidence for a benefit, not a sale price.
        if re.search(r"\b(?:value|worth)\b", lower):
            return None
        if len(prices) != 1:
            return None
        discount_price = prices[0]
        regular_price = None
        amount = None
        percent = None
    return {"raw": line[:4000], "service_hint": service, "regular_price": regular_price, "discount_price": discount_price, "discount_amount": amount, "discount_percent": percent, "price_model": model, "quantity": quantity}


def dedup_key(row: dict[str, Any]) -> tuple[Any, ...]:
    raw = re.sub(r"\[[^\]]*\]\([^)]*\)", "", row["raw"])
    raw = re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()
    return (row["source_type"], row["source_id"], row.get("service_id"), row.get("regular_price"), row.get("discount_price"), row.get("discount_amount"), row.get("discount_percent"), row.get("price_model"), row.get("quantity"), raw)


def service_for_hint(services: list[dict[str, Any]], hint: str) -> dict[str, Any] | None:
    aliases = {"filler": ("filler",), "neurotoxin": ("botox", "neurotoxin", "dysport", "xeomin", "jeuveau", "letybo"), "botox": ("botox",), "dysport": ("dysport",), "daxxify": ("daxxify",), "xeomin": ("xeomin",), "jeuveau": ("jeuveau",), "letybo": ("letybo",), "sculptra": ("sculptra",), "juvederm": ("juvederm",), "restylane": ("restylane",), "radiesse": ("radiesse",), "kybella": ("kybella",), "skinvive": ("skinvive",)}
    terms = aliases.get(hint, (hint,))
    exact = [row for row in services if any(term in str(row.get("service_name") or "").casefold() for term in terms)]
    if exact:
        return sorted(exact, key=lambda row: int(row["service_id"]))[0]
    if hint in {"botox", "dysport", "daxxify", "xeomin", "jeuveau", "letybo", "neurotoxin"}:
        neuro = [row for row in services if str(row.get("service_category") or "").casefold() == "neurotoxin"]
        if len(neuro) == 1:
            return neuro[0]
    if hint in {"filler", "sculptra", "juvederm", "restylane", "radiesse", "skinvive"}:
        fillers = [row for row in services if str(row.get("service_category") or "").casefold() in {"filler", "dermal filler", "biostimulator"}]
        if len(fillers) == 1:
            return fillers[0]
    return None


def build_plan() -> dict[str, Any]:
    client = load_supabase_client(ROOT)
    promotions = [r for r in fetch_all_rows(client, "clinic_promotions", "promotion_id,business_id,promotion_title,promotion_content,source_url,campaign_end_date,is_offer_extracted", order="promotion_id.asc") if int(r["promotion_id"]) in PROMOTION_IDS]
    memberships = [r for r in fetch_all_rows(client, "clinic_memberships", "plan_id,business_id,membership_name,benefits,source_url,is_offer_extracted", order="plan_id.asc") if int(r["plan_id"]) in MEMBERSHIP_IDS]
    services = fetch_all_rows(client, "clinic_services", "service_id,business_id,service_name,unit_type,service_category,regular_price", order="service_id.asc")
    offers = fetch_all_rows(client, "promo_offer_master", "id,business_id,promotion_id,membership_plan_id,offer_fingerprint", order="id.asc")
    existing_fps = {str(r.get("offer_fingerprint") or "") for r in offers}
    plan: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for kind, rows, source_key in (("promotion", promotions, "promotion_id"), ("membership", memberships, "plan_id")):
        for source in rows:
            source_id, business_id = int(source[source_key]), int(source["business_id"])
            content = source.get("promotion_content") if kind == "promotion" else source.get("benefits")
            source_url = source.get("source_url") or ""
            for line in source_lines(content):
                parsed = parse_line(line)
                if not parsed:
                    continue
                service = service_for_hint([s for s in services if int(s["business_id"]) == business_id], parsed["service_hint"])
                membership_plan_id = source_id if kind == "membership" else None
                promotion_id = source_id if kind == "promotion" else None
                service_name = service["service_name"] if service else parsed["service_hint"]
                unit_type = service.get("unit_type", "") if service else ""
                fp = compute_offer_fingerprint(source_url=source_url, service_name=service_name, unit_type=unit_type, regular_price=parsed["regular_price"], discount_price=parsed["discount_price"], offer_raw_text=parsed["raw"], business_id=business_id, promotion_id=promotion_id, membership_plan_id=membership_plan_id, discount_percent=parsed["discount_percent"], discount_amount=parsed["discount_amount"])
                row = {**parsed, "source_type": kind, "source_id": source_id, "business_id": business_id, "source_url": source_url, "promotion_id": promotion_id, "membership_plan_id": membership_plan_id, "service_id": int(service["service_id"]) if service else None, "service_name": service_name, "fingerprint": fp, "mapping_status": "mapped" if service else "unmapped", "status": "ready" if fp not in existing_fps else "already_exists"}
                row_key = dedup_key(row)
                if row_key not in seen:
                    seen.add(row_key)
                    plan.append(row)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "promotion_source_count": len(promotions), "membership_source_count": len(memberships), "offers": plan}


def apply_plan(client: Any, plan: dict[str, Any]) -> dict[str, int]:
    created_offers = created_items = 0
    for row in plan["offers"]:
        if row["status"] != "ready":
            continue
        master = {"business_id": row["business_id"], "promotion_id": row["promotion_id"], "membership_plan_id": row["membership_plan_id"], "regular_price": row["regular_price"], "discount_price": row["discount_price"], "discount_amount": row["discount_amount"], "discount_percent": row["discount_percent"], "is_membership_required": row["membership_plan_id"] is not None, "offer_fingerprint": row["fingerprint"], "offer_raw_text": row["raw"], "is_active": True, "is_new_customer_required": False, "price_model": row["price_model"]}
        offer_id = int(client.insert_rows("promo_offer_master", [master])[0]["id"])
        client.insert_rows("promo_offer_items", [{"offer_id": offer_id, "service_id": row["service_id"], "quantity": row["quantity"]}])
        created_offers += 1
        created_items += 1
    return {"created_offers": created_offers, "created_items": created_items}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    args = parser.parse_args()
    plan = build_plan()
    ready = [r for r in plan["offers"] if r["status"] == "ready"]
    plan["summary"] = {"ready": len(ready), "already_exists": sum(r["status"] == "already_exists" for r in plan["offers"]), "unmapped_items": sum(r.get("mapping_status") == "unmapped" for r in ready), "source_ids_with_candidates": len({(r["source_type"], r["source_id"]) for r in ready})}
    if args.apply:
        plan["applied"] = apply_plan(load_supabase_client(ROOT), plan)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"upstream_offer_extraction_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(path), "summary": plan["summary"], "applied": plan.get("applied")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
