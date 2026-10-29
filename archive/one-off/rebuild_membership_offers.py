#!/usr/bin/env python3
"""Rebuild promo_offer_master/items from repaired clinic_memberships.benefits.

Reads repairs manifest + current DB export of offers/services, writes
output/membership_offer_rebuild.sql and optionally applies via supabase CLI.

Usage:
  # First export services/offers (or pass --use-mcp-exports paths)
  python3 one-off/rebuild_membership_offers.py --dry-run
  python3 one-off/rebuild_membership_offers.py --write-sql
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "output" / "membership_benefits_repairs.json"
DEFAULT_SQL = ROOT / "output" / "membership_offer_rebuild.sql"
DEFAULT_ACTIONS = ROOT / "output" / "membership_offer_rebuild_actions.json"

BENEFIT_CATEGORY_PATTERNS = [
    (r"\ball\s+services\b", "All Services"),
    (r"\bskin\s*care\s+product", "Skincare Products"),
    (r"\ball\s+laser\s+service", "Laser Services"),
    (r"\bskin\s+service|\bmicroneedl", "Microneedling"),
    (r"\bfillers?\b", "Dermal Filler"),
]

SERVICE_PATTERNS = [
    (r"\bbotox\b", "Botox"),
    (r"\bdysport\b", "Dysport"),
    (r"\bjeuveau\b", "Jeuveau"),
    (r"\bdaxxify\b|\bdaxxi\b", "Daxxify"),
    (r"\bletybo\b", "Letybo"),
    (r"\bxeomin\b", "Xeomin"),
    (r"\bsculptra\b", "Sculptra"),
    (r"\bskinvive\b", "SkinVive"),
    (r"\bjuvederm\b|\bjuv[eé]derm\b", "Juvéderm"),
    (r"\bradiesse\b", "Radiesse"),
    (r"\bhydrafacial\b|\bdiamond\s*glow\b", "HydraFacial"),
    (r"\bmicroneedl", "Microneedling"),
    (r"\bchemical\s*peel|\bvi\s*peel\b", "Chemical Peel"),
    (r"\bprf\b", "PRF"),
    (r"\bfiller", "Dermal Filler"),
    (r"\bneuromodulator|\btox\b|\binjectable", "Botox"),  # fallback last
]


def money(s: Any) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(Decimal(str(s).replace(",", "").replace("$", "").strip()))
    except (InvalidOperation, ValueError):
        return None


def norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9$%]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def detect_benefit_category(text: str) -> Optional[str]:
    t = text.lower()
    for pat, name in BENEFIT_CATEGORY_PATTERNS:
        if re.search(pat, t):
            return name
    return None


def resolve_service_id(
    biz_id: int, service_name: Optional[str], services_by_biz: dict[int, dict[str, int]]
) -> Optional[int]:
    if not service_name:
        return None
    m = services_by_biz.get(biz_id, {})
    return m.get(service_name.lower()) or m.get(service_name)


def item_target(c: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return (service_name, item_name fallback when service_id cannot be resolved)."""
    sn = c.get("service_name")
    if sn:
        return sn, sn
    cat = detect_benefit_category(c.get("offer_raw_text") or "")
    if cat:
        return cat, cat
    return None, (c.get("offer_raw_text") or "")[:120] or None


def detect_service(text: str) -> Optional[str]:
    t = text.lower()
    for pat, name in SERVICE_PATTERNS:
        if re.search(pat, t):
            # skip overly broad injectables -> Botox fallback unless no better
            if name == "Botox" and pat.startswith(r"\bneuromodulator") and re.search(
                r"botox|dysport|jeuveau|daxxify|xeomin|letybo", t
            ):
                continue
            if name == "Botox" and "injectable" in pat and re.search(
                r"botox|dysport|filler|sculptra", t
            ):
                # prefer more specific if present
                for pat2, name2 in SERVICE_PATTERNS[:-1]:
                    if re.search(pat2, t):
                        return name2
            return name
    return None


def sql_quote(s: Optional[str]) -> str:
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def sql_num(v: Optional[float]) -> str:
    if v is None:
        return "NULL"
    return str(v)


def parse_benefit(text: str) -> Optional[dict[str, Any]]:
    """Parse a single benefit string into a priced offer candidate, or None if non-priced."""
    raw = text.strip()
    if not raw:
        return None
    # Skip soft perks / non-priced
    if re.search(
        r"priority|birthday|vip\s+event|loyalty\s+points|roll\s+over|gift\s+1\s+credit|"
        r"no\s+maximum|complimentary\s+wellness|first\s+access|exclusive\s+member|"
        r"beauty\s+bank\s+funds|monthly\s+credits\s+usable|laser\s+hair\s+removal\s+treatments\s+for",
        raw,
        re.I,
    ) and not re.search(r"\$\s*\d|\d+\s*%\s*off", raw):
        return None

    svc = detect_service(raw) or detect_benefit_category(raw)

    # $X/unit ... (regularly $Y)
    m = re.search(
        r"\$\s*(?P<disc>\d+(?:\.\d+)?)\s*(?:/\s*unit|/equivalent\s+dosing|a\s+unit|\s+per\s+unit)"
        r".*?(?:regularly|reg\.?)\s*\$\s*(?P<reg>\d+(?:\.\d+)?)",
        raw,
        re.I,
    )
    if m:
        disc, reg = money(m.group("disc")), money(m.group("reg"))
        return {
            "offer_raw_text": raw,
            "service_name": svc,
            "price_model": "per_unit",
            "discount_price": disc,
            "regular_price": reg,
            "discount_amount": round(reg - disc, 2) if reg and disc and reg > disc else None,
            "discount_percent": None,
        }

    # Service $X/unit or $X/unit Service
    m = re.search(
        r"(?:\$\s*(?P<disc>\d+(?:\.\d+)?)\s*/\s*unit)|(?:\$\s*(?P<disc2>\d+(?:\.\d+)?)\s*a\s+unit)",
        raw,
        re.I,
    )
    if m and re.search(r"unit", raw, re.I):
        disc = money(m.group("disc") or m.group("disc2"))
        return {
            "offer_raw_text": raw,
            "service_name": svc,
            "price_model": "per_unit",
            "discount_price": disc,
            "regular_price": None,
            "discount_amount": None,
            "discount_percent": None,
        }

    # $1 off each unit / $N off per unit
    m = re.search(r"\$\s*(?P<amt>\d+(?:\.\d+)?)\s*off\s*(?:per\s+)?(?:each\s+)?unit", raw, re.I)
    if m:
        return {
            "offer_raw_text": raw,
            "service_name": svc,
            "price_model": "per_unit",
            "discount_price": None,
            "regular_price": None,
            "discount_amount": money(m.group("amt")),
            "discount_percent": None,
        }

    # N% OFF ...
    m = re.search(r"(?P<pct>\d+(?:\.\d+)?)\s*%\s*off", raw, re.I)
    if m:
        pct = float(m.group("pct"))
        # optionally regularly $X for derived discount_price
        reg_m = re.search(r"regularly\s*\$\s*(?P<reg>\d+(?:\.\d+)?)", raw, re.I)
        reg = money(reg_m.group("reg")) if reg_m else None
        disc = round(reg * (1 - pct / 100), 2) if reg is not None else None
        amt = round(reg - disc, 2) if reg is not None and disc is not None else None
        return {
            "offer_raw_text": raw,
            "service_name": svc,
            "price_model": "total",
            "discount_price": disc,
            "regular_price": reg,
            "discount_amount": amt,
            "discount_percent": pct,
        }

    # $X OFF ...
    m = re.search(r"\$\s*(?P<amt>\d+(?:\.\d+)?)\s*off\b", raw, re.I)
    if m:
        amt = money(m.group("amt"))
        starts = re.search(r"starts?\s*at\s*\$\s*(?P<reg>\d+(?:\.\d+)?)", raw, re.I)
        reg = money(starts.group("reg")) if starts else None
        disc = round(reg - amt, 2) if reg and amt else None
        return {
            "offer_raw_text": raw,
            "service_name": svc,
            "price_model": "total",
            "discount_price": disc,
            "regular_price": reg,
            "discount_amount": amt,
            "discount_percent": None,
        }

    # fixed member price "$199 HydraFacial"
    m = re.search(r"\$\s*(?P<p>\d+(?:\.\d+)?)\s+(?P<rest>.+)", raw)
    if m and svc:
        return {
            "offer_raw_text": raw,
            "service_name": svc,
            "price_model": "total",
            "discount_price": money(m.group("p")),
            "regular_price": None,
            "discount_amount": None,
            "discount_percent": None,
        }

    return None


def match_offer(cand: dict, old_offers: list[dict], used: set[int]) -> Optional[int]:
    cn = norm_text(cand["offer_raw_text"])
    avail = [o for o in old_offers if o["id"] not in used]
    best = None
    best_score = 0
    for o in avail:
        on = norm_text(o.get("offer_raw_text") or "")
        score = 0
        if cn == on:
            score = 100
        else:
            a, b = set(cn.split()), set(on.split())
            if a and b:
                j = len(a & b) / len(a | b)
                score = int(j * 80)
            # unit price match boost
            if cand.get("discount_price") and o.get("discount_price"):
                if abs(float(cand["discount_price"]) - float(o["discount_price"])) < 0.02:
                    score += 15
            if cand.get("discount_percent") and o.get("discount_percent"):
                if abs(float(cand["discount_percent"]) - float(o["discount_percent"])) < 0.1:
                    score += 10
        if score > best_score:
            best_score = score
            best = o["id"]
    if best_score >= 40:
        return best
    return None


def build_actions(
    repairs: list[dict],
    offers_by_plan: dict[int, list[dict]],
    services_by_biz: dict[int, dict[str, int]],
) -> list[dict]:
    actions = []
    for r in repairs:
        if r.get("action") not in ("UPDATE", "CLEAR_UNSUPPORTED"):
            continue
        plan_id = int(r["plan_id"])
        biz_id = int(r["business_id"])
        old = offers_by_plan.get(plan_id, [])
        benefits = r.get("new_benefits") or []
        cands = []
        for b in benefits:
            c = parse_benefit(b)
            if not c:
                continue
            sn, iname = item_target(c)
            c["service_name"] = sn or c.get("service_name")
            c["item_name"] = iname
            c["service_id"] = resolve_service_id(biz_id, c.get("service_name"), services_by_biz)
            c["business_id"] = biz_id
            c["membership_plan_id"] = plan_id
            cands.append(c)

        used: set[int] = set()
        matched: set[int] = set()
        for c in cands:
            oid = match_offer(c, old, used)
            if oid is not None:
                used.add(oid)
                matched.add(oid)
                actions.append(
                    {
                        "action": "UPDATE",
                        "plan_id": plan_id,
                        "offer_id": oid,
                        "candidate": c,
                    }
                )
            else:
                actions.append(
                    {
                        "action": "INSERT",
                        "plan_id": plan_id,
                        "offer_id": None,
                        "candidate": c,
                    }
                )
        for o in old:
            if o["id"] not in matched and o.get("is_active"):
                actions.append(
                    {
                        "action": "DEACTIVATE",
                        "plan_id": plan_id,
                        "offer_id": o["id"],
                        "candidate": None,
                        "old_raw": o.get("offer_raw_text"),
                    }
                )
        actions.append({"action": "MARK_EXTRACTED", "plan_id": plan_id})
    return actions


def item_unit_price(c: dict[str, Any]) -> Optional[float]:
    up = c.get("discount_price")
    if c.get("price_model") == "total" and c.get("discount_amount") and up is None:
        return None
    return up


def render_item_insert_sql(offer_id_ref: str, c: dict[str, Any]) -> str:
    sid = c.get("service_id")
    iname = c.get("item_name") or c.get("service_name")
    up = item_unit_price(c)
    sid_sql = str(sid) if sid else "NULL"
    return (
        f"INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, unit_price)\n"
        f"SELECT {offer_id_ref}, {sid_sql}, {sql_quote(iname)}, 1, {sql_num(up)};"
    )


def render_item_sql(offer_id_ref: str, c: dict[str, Any]) -> list[str]:
    """Always emit at least one promo_offer_items row for an offer."""
    sid = c.get("service_id")
    iname = c.get("item_name") or c.get("service_name")
    up = item_unit_price(c)
    sid_sql = str(sid) if sid else "NULL"
    return [
        f"INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, unit_price)\n"
        f"SELECT {offer_id_ref}, {sid_sql}, {sql_quote(iname)}, 1, {sql_num(up)}\n"
        f"WHERE NOT EXISTS (\n"
        f"  SELECT 1 FROM public.promo_offer_items WHERE offer_id = {offer_id_ref}\n"
        f");\n"
        f"UPDATE public.promo_offer_items SET\n"
        f"  service_id = {sid_sql},\n"
        f"  item_name = {sql_quote(iname)},\n"
        f"  quantity = coalesce(quantity, 1),\n"
        f"  unit_price = {sql_num(up)},\n"
        f"  updated_at = now()\n"
        f"WHERE offer_id = {offer_id_ref};"
    ]


def render_sql(actions: list[dict]) -> str:
    parts = ["-- membership offer rebuild", "-- apply via: supabase db query --linked -f ...", ""]
    for a in actions:
        if a["action"] == "DEACTIVATE":
            parts.append(
                f"UPDATE public.promo_offer_master SET is_active = false, updated_at = now() "
                f"WHERE id = {a['offer_id']} AND membership_plan_id = {a['plan_id']};"
            )
        elif a["action"] == "MARK_EXTRACTED":
            parts.append(
                f"UPDATE public.clinic_memberships SET is_offer_extracted = true, updated_at = now() "
                f"WHERE plan_id = {a['plan_id']};"
            )
        elif a["action"] in ("UPDATE", "INSERT"):
            c = a["candidate"]
            fields = (
                f"offer_raw_text = {sql_quote(c['offer_raw_text'])},\n"
                f"  price_model = {sql_quote(c['price_model'])},\n"
                f"  discount_price = {sql_num(c.get('discount_price'))},\n"
                f"  regular_price = {sql_num(c.get('regular_price'))},\n"
                f"  discount_amount = {sql_num(c.get('discount_amount'))},\n"
                f"  discount_percent = {sql_num(c.get('discount_percent'))},\n"
                f"  is_membership_required = true,\n"
                f"  is_new_customer_required = false,\n"
                f"  is_active = true,\n"
                f"  updated_at = now()"
            )
            if a["action"] == "UPDATE":
                parts.append(
                    f"-- UPDATE offer {a['offer_id']} plan {a['plan_id']}\n"
                    f"UPDATE public.promo_offer_master SET\n  {fields}\n"
                    f"WHERE id = {a['offer_id']} AND membership_plan_id = {a['plan_id']};"
                )
                parts.extend(render_item_sql(str(a["offer_id"]), c))
            else:
                parts.append(
                    f"-- INSERT offer plan {a['plan_id']}\n"
                    f"WITH ins AS (\n"
                    f"  INSERT INTO public.promo_offer_master (\n"
                    f"    business_id, membership_plan_id, offer_raw_text, price_model,\n"
                    f"    discount_price, regular_price, discount_amount, discount_percent,\n"
                    f"    is_membership_required, is_new_customer_required, is_active, moderation_status\n"
                    f"  ) VALUES (\n"
                    f"    {c['business_id']}, {c['membership_plan_id']}, {sql_quote(c['offer_raw_text'])}, {sql_quote(c['price_model'])},\n"
                    f"    {sql_num(c.get('discount_price'))}, {sql_num(c.get('regular_price'))}, {sql_num(c.get('discount_amount'))}, {sql_num(c.get('discount_percent'))},\n"
                    f"    true, false, true, 'approved'\n"
                    f"  ) RETURNING id\n"
                    f")\n"
                    f"{render_item_insert_sql('ins.id', c).replace(';', ' FROM ins;')}\n"
                )
        parts.append("")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--offers-json", type=Path, default=ROOT / "output" / "membership_offers_export.json")
    ap.add_argument("--services-json", type=Path, default=ROOT / "output" / "membership_services_export.json")
    ap.add_argument("--sql-out", type=Path, default=DEFAULT_SQL)
    ap.add_argument("--actions-out", type=Path, default=DEFAULT_ACTIONS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--write-sql", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not args.write_sql:
        args.dry_run = True

    manifest = json.loads(args.manifest.read_text())
    repairs = manifest["repairs"] if isinstance(manifest, dict) else manifest
    offers = json.loads(args.offers_json.read_text())
    services = json.loads(args.services_json.read_text())

    offers_by_plan: dict[int, list] = defaultdict(list)
    for o in offers:
        if o.get("membership_plan_id") is not None:
            offers_by_plan[int(o["membership_plan_id"])].append(o)

    services_by_biz: dict[int, dict[str, int]] = defaultdict(dict)
    for s in services:
        services_by_biz[int(s["business_id"])][s["service_name"].lower()] = int(s["service_id"])
        services_by_biz[int(s["business_id"])][s["service_name"]] = int(s["service_id"])

    actions = build_actions(repairs, offers_by_plan, services_by_biz)
    args.actions_out.write_text(json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8")

    from collections import Counter

    ctr = Counter(a["action"] for a in actions)
    print(json.dumps({"actions": dict(ctr), "total": len(actions)}, indent=2))

    if args.write_sql or args.dry_run:
        sql = render_sql(actions)
        args.sql_out.write_text(sql, encoding="utf-8")
        print(f"wrote {args.sql_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
