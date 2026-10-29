#!/usr/bin/env python3
"""Round-2 repair for the clinic membership and membership-offer plan.

The script is a frozen dry-run/apply operation.  It covers the remaining
source-backed membership fixes, creates only atomic service Offers, fills
membership Offer conditions, and fixes item names from their service rows.
Unmapped broad/category benefits remain false or review rather than becoming
invented single-service Offers.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.offer_fingerprint import compute_offer_fingerprint
from utils.clinic_service_taxonomy import service_name_enum_hint
from utils.schema_contract import OFFER_MASTER_SELECT
from utils.supabase_access import fetch_all_rows, load_supabase_client

ARTIFACT_DIR = ROOT / ".firecrawl" / "clinic_memberships_plan_20260917"
ARTIFACT = ARTIFACT_DIR / "round2_controlled_apply.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def unique(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = norm(text)
        if text and key not in seen:
            out.append(text)
            seen.add(key)
    return out


def eq_filter(column: str, value: Any) -> Dict[str, str]:
    return {column: "is.null" if value is None else f"eq.{value}"}


def current_memberships(client: Any) -> List[Dict[str, Any]]:
    return fetch_all_rows(
        client,
        "clinic_memberships",
        "plan_id,business_id,membership_name,membership_price,billing_period,minimum_commitment_months,updated_at,benefits,source_url,is_offer_extracted,is_ocr_required,need_browser",
        order="plan_id.asc",
    )


def row_by_id(rows: List[Dict[str, Any]], plan_id: int) -> Dict[str, Any]:
    for row in rows:
        if int(row["plan_id"]) == plan_id:
            return row
    raise RuntimeError(f"missing plan {plan_id}")


def before_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in (
        "plan_id", "business_id", "membership_name", "membership_price",
        "billing_period", "minimum_commitment_months", "updated_at", "benefits",
        "source_url", "is_offer_extracted", "is_ocr_required", "need_browser",
    )}


def membership_payloads(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    current = {int(row["plan_id"]): row for row in rows}
    payloads: Dict[int, Dict[str, Any]] = {
        696: {
            "membership_price": 134.0,
            "billing_period": "monthly",
            "benefits": [
                "$134 per month",
                "One-time startup fee of $433",
                "Up to 4 Botox or Jeuveau treatments every 90 days",
                "Up to 44 units per treatment",
                "Save up to $900 annually",
                "$50 off JUVEDERM syringes at the 3rd and 4th appointments",
                "Discounted extra units at $10 versus $13 for non-members",
                "Member-only appointment slots",
                "No deposit required for appointments",
                "Exclusive CC VIP perks",
            ],
            "is_offer_extracted": False,
            "is_ocr_required": False,
            "need_browser": False,
        },
        697: {
            "membership_price": 234.0,
            "billing_period": "monthly",
            "benefits": [
                "$234 billed monthly",
                "One-time startup fee of $633",
                "Up to 4 Botox or Jeuveau treatments every 90 days",
                "Up to 44 units per treatment",
                "Free JUVEDERM syringe at the 2nd and 4th appointments",
                "$50 off a JUVEDERM syringe at the 3rd appointment",
                "Reduced extra-unit rate of $10 versus $13 for non-members",
                "Save up to $1,350 annually",
                "Member-only appointment slots",
                "No deposit required for appointments",
                "10% off lasers, microneedling, facials, body contouring, and skincare",
                "Exclusive CC VIP perks",
            ],
            "is_offer_extracted": False,
            "is_ocr_required": False,
            "need_browser": False,
        },
        319: {
            "minimum_commitment_months": 6,
            "benefits": [
                "$149 per month",
                "$149 monthly payment becomes membership credit",
                "Credit can be used for eligible treatments",
                "Unused credits roll over",
                "Credits never expire",
                "No initiation fee",
                "No cancellation fee",
                "Minimum 6-month commitment",
                "Save 10% to 30% on Fillers, Botox, PRF, Chemical Peels, Kybella, and Microneedling",
            ],
            "is_offer_extracted": False,
        },
        440: {
            "benefits": unique([
                *current[440].get("benefits", []),
                "First month free",
                "No contract",
                "Cancel anytime",
            ]),
            "minimum_commitment_months": 0,
            "is_offer_extracted": False,
        },
        236: {
            "is_offer_extracted": False,
            "is_ocr_required": False,
            "need_browser": False,
        },
    }

    grace = "Beauty Wallet credits remain available during the post-cancellation grace period; credits do not expire during that grace period"
    for plan_id in (422, 423, 424, 425, 426):
        payloads[plan_id] = {
            "benefits": unique(
                [
                    item for item in current[plan_id].get("benefits", [])
                    if "do not expire after cancellation" not in norm(item)
                ]
                + [grace]
            )
        }

    # The browser page exposes one current Fresh Face Club image, but does not
    # map its four packages to the six legacy plan rows.  Replace stale image
    # placeholders with a source-backed review note and preserve the explicit
    # browser/OCR state until a tier mapping is supplied.
    review_note = "Fresh Face Club package menu OCR captured; current page does not identify the package-to-tier mapping for this plan"
    for plan_id in (461, 462):
        payloads[plan_id] = {
            "benefits": [review_note],
            "is_offer_extracted": False,
            "is_ocr_required": True,
            "need_browser": True,
        }
    return payloads


def service_inserts() -> List[Dict[str, Any]]:
    return [
        {"business_id": 3022, "service_name": "Glo2 Facial", "regular_price": 199.0, "unit_type": "session", "source_url": "https://houseofaestheticsmedspa.com/glo2-facial-huntington-beach/"},
        {"business_id": 3020, "service_name": "Juvéderm", "regular_price": None, "unit_type": "syringe", "source_url": "https://californiacosmetics.com/products/botox-more-member"},
        {"business_id": 3020, "service_name": "IPL Photofacial", "regular_price": None, "unit_type": "session", "source_url": "https://californiacosmetics.com/pages/med-spa-memberships"},
        {"business_id": 2995, "service_name": "Hydrafacial", "regular_price": None, "unit_type": "session", "source_url": "https://skinmaison.com/beauty-bank"},
        {"business_id": 2998, "service_name": "Hydrafacial", "regular_price": None, "unit_type": "session", "source_url": "https://www.newportbeachmedspa.com/memberships/"},
        {"business_id": 3157, "service_name": "DiamondGlow", "regular_price": None, "unit_type": "session", "source_url": "https://www.botoxandbeauty.com/membership/"},
    ]


def offer_specs() -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []

    def add(plan: int, service: str, raw: str, **fields: Any) -> None:
        specs.append({"plan_id": plan, "service_name": service, "offer_raw_text": raw, **fields})

    # California Cosmetics: independent service/tier Offers.
    for plan in (696, 697):
        for service in ("Botox", "Jeuveau"):
            add(plan, service, f"{plan} membership: up to 4 {service} treatments every 90 days", discount_price=0.0, discount_percent=100.0, quantity=1, condition="Up to 4 treatments every 90 days")
        add(plan, "Botox", f"{plan} membership: extra Botox units $10 versus $13 for non-members", regular_price=13.0, discount_price=10.0, price_model="per_unit", unit_price=10.0, condition="Extra units only")
        add(plan, "Jeuveau", f"{plan} membership: extra Jeuveau units $10 versus $13 for non-members", regular_price=13.0, discount_price=10.0, price_model="per_unit", unit_price=10.0, condition="Extra units only")
    add(696, "Juvéderm", "TOX Membership: $50 off JUVEDERM syringes at the 3rd and 4th appointments", discount_amount=50.0, condition="3rd and 4th appointments")
    add(697, "Juvéderm", "TOX & More Membership: free JUVEDERM syringe at the 2nd and 4th appointments", discount_price=0.0, discount_percent=100.0, condition="2nd and 4th appointments")
    add(697, "Juvéderm", "TOX & More Membership: $50 off a JUVEDERM syringe at the 3rd appointment", discount_amount=50.0, condition="3rd appointment")
    add(698, "Hydrafacial", "Deluxe HydraFacial Membership: 1 HydraFacial treatment every month", discount_price=0.0, discount_percent=100.0, quantity=1, condition="1 treatment per month")
    for service in ("PRP", "Hydrafacial", "IPL Photofacial"):
        add(699, service, f"Skin Goals Membership: selectable {service} med-spa experience", discount_price=0.0, discount_percent=100.0, quantity=1, condition="Selectable treatment within four med-spa experiences")

    # Skin Maison and The Fix.
    add(700, "Hydrafacial", "Deluxe Hydrafacial Membership: Signature HydraFacial with customized serum and LED light therapy", discount_price=0.0, discount_percent=100.0, quantity=1, condition="Signature treatment; 45 minutes")
    add(700, "Neurotoxin", "Deluxe Hydrafacial Membership: Tox $2 off per unit", discount_amount=2.0, price_model="per_unit", condition="Tox units")
    add(700, "Dermal Filler", "Deluxe Hydrafacial Membership: Injectables $100 off", discount_amount=100.0)
    for service in ("Botox", "Dysport", "Xeomin", "Daxxify"):
        add(701, service, f"Botox Membership: {service} save $2 per unit", discount_amount=2.0, price_model="per_unit", condition="Monthly contribution options: $150, $200, $250, or $300")

    # These are discount-only per-unit benefits.  The source provides the
    # savings amount but no member sale price, so keep discount_price null.
    add(409, "Neurotoxin", "Essential Membership: Tox $1 off per unit", discount_amount=1.0, price_model="per_unit", condition="Member pricing")
    add(409, "Dysport", "Essential Membership: Dysport $0.50 off per unit", discount_amount=0.5, price_model="per_unit", condition="Member pricing")
    add(410, "Neurotoxin", "Elite Membership: Tox $2 off per unit", discount_amount=2.0, price_model="per_unit", condition="Member pricing")
    add(410, "Dysport", "Elite Membership: Dysport $2 off per unit", discount_amount=2.0, price_model="per_unit", condition="Member pricing")

    # Explicit member prices that were previously disabled by the package
    # price cleanup; each service gets its own Offer.
    for service in ("Botox", "Dysport"):
        add(239, service, f"Signature GLO Up Membership: $11 per unit {service}", discount_price=11.0, price_model="per_unit", unit_price=11.0, condition="Member pricing")
    add(452, "Daxxify", "B Rejuvenated Membership: Daxxify $12/unit (regularly $14/unit)", regular_price=14.0, discount_price=12.0, price_model="per_unit", unit_price=12.0, condition="Member pricing")
    add(453, "Daxxify", "B Transformed Membership: Daxxify $11/unit (regularly $14/unit)", regular_price=14.0, discount_price=11.0, price_model="per_unit", unit_price=11.0, condition="Member pricing")

    add(486, "Hydrafacial", "HydraFacial Membership: deep cleanse, extract, hydrate, with add-on light therapy", discount_price=0.0, discount_percent=100.0, quantity=1, condition="Included treatment; add-on light therapy")

    # House of Aesthetics Glo2 Society.
    add(702, "Glo2 Facial", "Glo2 Society Membership: 1 Glo2 Facial per month", discount_price=0.0, discount_percent=100.0, quantity=1, condition="1 Glo2 Facial per month; unused treatments roll over up to 60 days")

    # Existing plans whose explicit service benefits were missing Offers.
    add(289, "Neurotoxin", "Skinjectables Vault Membership: Tox as low as $9 per unit", discount_price=9.0, price_model="per_unit", unit_price=9.0, condition="Tox as low as $9/unit")
    for service in ("Hydrafacial", "Glo2 Facial"):
        add(300, service, f"Monthly Glow Membership: 1 {service} per month", discount_price=0.0, discount_percent=100.0, quantity=1, condition="Choose one monthly treatment")
    add(303, "Botox", "Botox Membership: $180 for up to 20 units of Botox", discount_price=180.0, quantity=20, condition="Up to 20 units")
    add(303, "Botox", "Botox Membership: $9 per Botox unit beyond the first 20 units", discount_price=9.0, price_model="per_unit", unit_price=9.0, condition="Units beyond the first 20")
    for service in ("Xeomin", "Daxxify"):
        add(396, service, f"Diamond membership: $1 off each unit of {service}", discount_amount=1.0, price_model="per_unit", condition="Member pricing")
    add(423, "Botox", "Starter $150: $14 per unit on Botox", regular_price=18.0, discount_price=14.0, price_model="per_unit", unit_price=14.0, condition="3-month commitment")
    add(424, "Botox", "Silver: $14 per unit on Botox", regular_price=18.0, discount_price=14.0, price_model="per_unit", unit_price=14.0, condition="1-year commitment")
    for plan in (425, 426):
        add(plan, "Botox", f"{plan} membership: $14 per unit on Botox", regular_price=18.0, discount_price=14.0, price_model="per_unit", unit_price=14.0, condition="1-year commitment")
        add(plan, "Dermal Filler", f"{plan} membership: 25% off aesthetic injectables", discount_percent=25.0, condition="Aesthetic injectables")
    for service in ("Botox", "Dysport", "Xeomin", "Daxxify"):
        add(428, service, f"Elite Membership: {service} save $2 per unit", discount_amount=2.0, price_model="per_unit", condition="Member-only pricing")
    add(449, "Botox", "Seranova Membership: 20 units of Botox every 3 months", discount_price=0.0, discount_percent=100.0, quantity=20, condition="Quarterly allowance; 20 units every 3 months")
    add(450, "Botox", "Seranova Body Membership: 20 units of Botox every 3 months", discount_price=0.0, discount_percent=100.0, quantity=20, condition="Quarterly allowance; 20 units every 3 months")
    for service in ("Botox", "Dysport"):
        add(519, service, f"Premium GLO Up Membership: $11 per unit {service}", discount_price=11.0, price_model="per_unit", unit_price=11.0, condition="Member pricing")
    add(657, "Botox", "BRILLIANT Membership: 5 units of neurotoxin per month", discount_price=0.0, discount_percent=100.0, quantity=5, condition="5 units per month")
    add(676, "DiamondGlow", "Diamond Glow Membership: one Diamond Glow facial treatment per month", discount_price=0.0, discount_percent=100.0, quantity=1, condition="One treatment per month")

    # Crystal/Diamond aggregate repair: each service gets its own master.
    for plan, prices in ((317, {"Botox": 13.5, "Dysport": 13.5, "Daxxify": 10.99}), (318, {"Botox": 12.8, "Dysport": 12.8, "Daxxify": 9.99})):
        for service, price in prices.items():
            add(plan, service, f"{plan} membership: {service} {price:g} per unit", discount_price=price, price_model="per_unit", unit_price=price, condition="Membership pricing")
    add(317, "Dermal Filler", "Crystal Membership: $50 off all filler syringes", discount_amount=50.0)
    add(318, "Dermal Filler", "Diamond Membership: $100 off all filler syringes", discount_amount=100.0)
    add(317, "Chemical Peel", "Crystal Membership: 20% off all chemical peels", discount_percent=20.0)
    add(318, "Chemical Peel", "Diamond Membership: 30% off all chemical peels", discount_percent=30.0)
    return specs


def current_offer(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in ("id", "business_id", "membership_plan_id", "promotion_id", "is_active", "updated_at", "offer_fingerprint", "conditions", "offer_raw_text")}


def desired_conditions(plan: Dict[str, Any], offer: Dict[str, Any]) -> List[str]:
    result = list(offer.get("conditions") or [])
    name = str(plan.get("membership_name") or "").strip()
    result.insert(0, f"Applicable membership tier: {name}")
    commitment = plan.get("minimum_commitment_months")
    if commitment is not None:
        result.append(f"Minimum commitment: {int(commitment)} months")
    raw = str(offer.get("offer_raw_text") or "")
    for pattern, text in (
        (r"every\s+90\s+days", "Redemption frequency: every 90 days"),
        (r"every\s+3\s+months|every\s+quarter", "Redemption frequency: every 3 months"),
        (r"per\s+month|monthly", "Redemption frequency: monthly"),
        (r"3rd\s+and\s+4th", "Appointment limit: 3rd and 4th appointments"),
        (r"2nd\s+and\s+4th", "Appointment limit: 2nd and 4th appointments"),
    ):
        if re.search(pattern, raw, re.I):
            result.append(text)
    return unique(result)


def build_plan(client: Any) -> Dict[str, Any]:
    rows = current_memberships(client)
    payloads = membership_payloads(rows)
    updates = [{"plan_id": pid, "before": before_row(row_by_id(rows, pid)), "payload": payload} for pid, payload in sorted(payloads.items())]
    existing_702 = [row for row in rows if int(row["business_id"]) == 3022 and norm(row["membership_name"]) == norm("Glo2 Society Membership")]
    new_plan = None if existing_702 else {
        "business_id": 3022,
        "membership_name": "Glo2 Society Membership",
        "membership_price": 160.0,
        "billing_period": "monthly",
        "minimum_commitment_months": 6,
        "benefits": [
            "$160 per month",
            "1 Glo2 Facial per month",
            "ZO Mask or Hydrojelly Mask",
            "10% off ZO Skin Health and Skinbetter Science skincare products monthly",
            "6-month minimum commitment",
            "Unused treatments roll over up to 60 days",
        ],
        "source_url": "https://houseofaestheticsmedspa.com/glo2-facial-huntington-beach/",
        "is_offer_extracted": False,
        "is_ocr_required": False,
        "need_browser": False,
    }
    services = fetch_all_rows(client, "clinic_services", "service_id,business_id,service_name,regular_price,unit_type", order="service_id.asc")
    service_keys = {(int(s["business_id"]), norm(s["service_name"])) for s in services}
    needed_inserts = [s for s in service_inserts() if (s["business_id"], norm(s["service_name"])) not in service_keys]
    offers = fetch_all_rows(client, "promo_offer_master", OFFER_MASTER_SELECT, filters={"membership_plan_id": "not.is.null"}, order="id.asc")
    by_plan = {int(row["plan_id"]): row for row in rows}
    condition_ops = []
    for offer in offers:
        if not offer.get("is_active") or offer.get("promotion_id") is not None:
            continue
        plan = by_plan.get(int(offer["membership_plan_id"]))
        if not plan:
            continue
        target = desired_conditions(plan, offer)
        if unique(offer.get("conditions") or []) != target:
            condition_ops.append({"before": current_offer(offer), "conditions": target})
    items = fetch_all_rows(client, "promo_offer_items", "offer_item_id,offer_id,item_name,service_id,updated_at", order="offer_item_id.asc")
    service_by_id = {int(s["service_id"]): s for s in services}
    item_ops = []
    for item in items:
        if item.get("service_id") is None or int(item["service_id"]) not in service_by_id:
            continue
        service_name = service_by_id[int(item["service_id"])]["service_name"]
        if item.get("item_name") != service_name:
            item_ops.append({"before": dict(item), "item_name": service_name})
    aggregates = []
    for offer in offers:
        if not offer.get("is_active") or int(offer.get("membership_plan_id") or 0) not in (317, 318):
            continue
        if re.search(r"botox\s*/\s*dysport|botox\s+and\s+dysport|botox/dysport", str(offer.get("offer_raw_text") or ""), re.I):
            aggregates.append(current_offer(offer))
    return {
        "created_at": now(),
        "membership_updates": updates,
        "new_glo2_plan": new_plan,
        "service_inserts": needed_inserts,
        "offer_specs": offer_specs(),
        "condition_updates": condition_ops,
        "item_name_updates": item_ops,
        "aggregate_offer_deactivations": aggregates,
        "review": [
            {"plan_ids": [461, 462], "reason": "Current Fresh Face Club image has no tier-to-package mapping; flags remain OCR/browser review."},
            {"plan_ids": [326, 333, 397, 416, 434, 435, 449, 450, 519, 656, 677], "reason": "Benefits include broad or selectable services without a complete same-business service mapping; extraction remains false until mapped."},
        ],
    }


def verify_before(actual: Dict[str, Any], before: Dict[str, Any], label: str) -> None:
    for key, value in before.items():
        if isinstance(value, list):
            if unique(actual.get(key) or []) != unique(value):
                raise RuntimeError(f"concurrency conflict {label}:{key}")
        elif actual.get(key) != value:
            raise RuntimeError(f"concurrency conflict {label}:{key}")


def service_map(client: Any) -> Dict[tuple[int, str], Dict[str, Any]]:
    rows = fetch_all_rows(client, "clinic_services", "service_id,business_id,service_name,regular_price,unit_type", order="service_id.asc")
    out: Dict[tuple[int, str], Dict[str, Any]] = {}
    for row in rows:
        out.setdefault((int(row["business_id"]), norm(row["service_name"])), row)
    return out


def membership_conditions(plan: Dict[str, Any], extra: Optional[str]) -> List[str]:
    values = [f"Applicable membership tier: {plan['membership_name']}"]
    if plan.get("minimum_commitment_months") is not None:
        values.append(f"Minimum commitment: {int(plan['minimum_commitment_months'])} months")
    if extra:
        values.append(extra)
    return unique(values)


def apply_offer_spec(client: Any, spec: Dict[str, Any], plans: Dict[int, Dict[str, Any]], services: Dict[tuple[int, str], Dict[str, Any]], existing: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan_id = int(spec["plan_id"])
    plan = plans.get(plan_id)
    if plan is None:
        raise RuntimeError(f"offer spec references missing plan {plan_id}")
    service = services.get((int(plan["business_id"]), norm(spec["service_name"])))
    if service is None:
        raise RuntimeError(f"missing service for {plan_id}: {spec['service_name']}")
    raw = spec["offer_raw_text"]
    price_model = spec.get("price_model") or ("per_unit" if re.search(r"per\s+unit|per\s+u(nit)?", raw, re.I) else "total")
    # A per-unit discount-only Offer (for example "$2 off per unit") is
    # representable without a sale unit price.  The activation guard only
    # requires unit_price when a normalized discount_price is present.
    offer_can_be_active = not (
        price_model == "per_unit"
        and spec.get("discount_price") is not None
        and spec.get("unit_price") is None
    )
    fp = compute_offer_fingerprint(
        source_url=str(plan.get("source_url") or ""),
        service_name=str(service["service_name"]),
        unit_type=str(service.get("unit_type") or ""),
        regular_price=spec.get("regular_price"),
        discount_price=spec.get("discount_price"),
        offer_raw_text=raw,
        business_id=int(plan["business_id"]),
        membership_plan_id=plan_id,
        discount_percent=spec.get("discount_percent"),
        discount_amount=spec.get("discount_amount"),
    )
    conditions = membership_conditions(plan, spec.get("condition"))
    payload = {
        "business_id": int(plan["business_id"]),
        "membership_plan_id": plan_id,
        "is_package": bool(spec.get("is_package", False)),
        "is_active": offer_can_be_active,
        "is_new_customer_required": False,
        "is_membership_required": True,
        "offer_fingerprint": fp,
        "offer_raw_text": raw,
        "price_model": price_model,
        "conditions": conditions,
        "updated_at": now(),
    }
    for field in ("regular_price", "discount_price", "discount_percent", "discount_amount"):
        if spec.get(field) is not None:
            payload[field] = spec[field]
    # Older rows may carry fingerprints computed before the current trigger
    # contract.  Prefer the same plan/raw text first so a rebuild updates the
    # existing Offer instead of creating a duplicate legacy row.
    matches = [
        row for row in existing
        if int(row.get("membership_plan_id") or 0) == plan_id
        and str(row.get("offer_raw_text") or "") == raw
    ]
    if not matches:
        matches = [row for row in existing if str(row.get("offer_fingerprint") or "") == fp]
    matches.sort(key=lambda row: (not bool(row.get("is_active")), int(row.get("id") or 0)))
    if matches:
        offer = matches[0]
        returned = client.update_row("promo_offer_master", {"id": f"eq.{offer['id']}"}, payload)
        offer_id = int(returned[0]["id"]) if returned else int(offer["id"])
    else:
        inserted = client.insert_rows("promo_offer_master", [payload])
        if len(inserted) != 1:
            raise RuntimeError(f"offer insert returned {len(inserted)}")
        offer_id = int(inserted[0]["id"])
    item = {"offer_id": offer_id, "service_id": int(service["service_id"]), "item_name": service["service_name"], "created_at": now(), "updated_at": now()}
    if spec.get("quantity") is not None:
        item["delivered_units"] = spec["quantity"]
    if spec.get("unit_price") is not None:
        item["unit_price"] = spec["unit_price"]
    current_items = client.fetch_rows(
        "promo_offer_items",
        "offer_item_id,service_id,item_name,delivered_units,unit_price",
        filters={"offer_id": f"eq.{offer_id}"},
        limit=20,
    )
    expected_quantity = spec.get("quantity")
    expected_unit_price = spec.get("unit_price")
    already_correct = (
        len(current_items) == 1
        and int(current_items[0].get("service_id") or 0) == int(service["service_id"])
        and str(current_items[0].get("item_name") or "") == str(service["service_name"])
        and current_items[0].get("delivered_units") == expected_quantity
        and current_items[0].get("unit_price") == expected_unit_price
    )
    if not already_correct:
        client.delete_rows("promo_offer_items", {"offer_id": f"eq.{offer_id}"})
        client.insert_rows("promo_offer_items", [item])
    # The live write path defaults newly inserted membership Offers to
    # inactive.  Activate only after the atomic item exists and the caller has
    # completed the membership-level extraction gate.
    client.update_row("promo_offer_master", {"id": f"eq.{offer_id}"}, {"is_active": offer_can_be_active, "updated_at": now()})
    existing[:] = [row for row in existing if str(row.get("offer_fingerprint") or "") != fp]
    existing.append({"id": offer_id, "offer_fingerprint": fp})
    return {"offer_id": offer_id, "plan_id": plan_id, "service_id": service["service_id"], "fingerprint": fp}


def apply_plan(client: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
    rows = current_memberships(client)
    by_id = {int(row["plan_id"]): row for row in rows}
    updated_plans: List[int] = []
    for operation in plan["membership_updates"]:
        pid = int(operation["plan_id"])
        row = by_id.get(pid)
        if row is None:
            raise RuntimeError(f"missing plan during apply: {pid}")
        payload = dict(operation["payload"])
        already_applied = all(
            unique(row.get(key) or []) == unique(value or []) if isinstance(value, list) else row.get(key) == value
            for key, value in payload.items()
        )
        if already_applied:
            updated_plans.append(pid)
            continue
        verify_before(row, operation["before"], f"plan {pid}")
        payload["updated_at"] = now()
        filters = {"plan_id": f"eq.{pid}", **eq_filter("updated_at", operation["before"].get("updated_at"))}
        result = client.update_row("clinic_memberships", filters, payload)
        if len(result) != 1:
            raise RuntimeError(f"plan {pid} update returned {len(result)}")
        updated_plans.append(pid)

    services = service_map(client)
    inserted_services: List[Dict[str, Any]] = []
    for payload in plan["service_inserts"]:
        canonical_name = service_name_enum_hint(payload.get("service_name"))
        if canonical_name is None:
            raise RuntimeError(f"unsupported clinic service enum: {payload.get('service_name')!r}")
        key = (int(payload["business_id"]), norm(canonical_name))
        if key in services:
            continue
        row = {**payload, "service_name": canonical_name}
        row["created_at"] = now()
        row["updated_at"] = row["created_at"]
        inserted = client.insert_rows("clinic_services", [row])
        if len(inserted) != 1:
            raise RuntimeError(f"service insert returned {len(inserted)}")
        services[key] = inserted[0]
        inserted_services.append({"service_id": inserted[0]["service_id"], "business_id": payload["business_id"], "service_name": payload["service_name"]})

    new_plan = plan.get("new_glo2_plan")
    inserted_plans: List[Dict[str, Any]] = []
    if new_plan:
        existing = fetch_all_rows(client, "clinic_memberships", "plan_id,membership_name,business_id", filters={"business_id": "eq.3022"})
        if any(norm(row["membership_name"]) == norm(new_plan["membership_name"]) for row in existing):
            raise RuntimeError("Glo2 Society appeared before apply")
        payload = dict(new_plan)
        payload["created_at"] = now()
        payload["updated_at"] = payload["created_at"]
        inserted = client.insert_rows("clinic_memberships", [payload])
        if len(inserted) != 1:
            raise RuntimeError("Glo2 plan insert failed")
        inserted_plans.append({"plan_id": inserted[0]["plan_id"], "membership_name": new_plan["membership_name"]})

    rows = current_memberships(client)
    plans = {int(row["plan_id"]): row for row in rows}
    # Resolve the newly created Glo2 plan id for its Offer spec.
    if 702 not in plans:
        glo2 = next((row for row in rows if norm(row["membership_name"]) == norm("Glo2 Society Membership") and int(row["business_id"]) == 3022), None)
        if glo2:
            for spec in plan["offer_specs"]:
                if int(spec["plan_id"]) == 702:
                    spec["plan_id"] = int(glo2["plan_id"])
    services = service_map(client)
    existing_offers = fetch_all_rows(client, "promo_offer_master", OFFER_MASTER_SELECT, filters={"membership_plan_id": "not.is.null"}, order="id.asc")
    created_offers: List[Dict[str, Any]] = []
    for spec in plan["offer_specs"]:
        created_offers.append(apply_offer_spec(client, spec, plans, services, existing_offers))

    deactivated: List[int] = []
    for offer in plan["aggregate_offer_deactivations"]:
        current = client.fetch_rows("promo_offer_master", "id,updated_at,is_active", filters={"id": f"eq.{offer['id']}"}, limit=1)
        if not current or not current[0].get("is_active"):
            continue
        if current[0].get("updated_at") != offer.get("updated_at"):
            raise RuntimeError(f"aggregate offer {offer['id']} changed before apply")
        result = client.update_row("promo_offer_master", {"id": f"eq.{offer['id']}", "updated_at": f"eq.{offer['updated_at']}"}, {"is_active": False, "updated_at": now()})
        if len(result) != 1:
            raise RuntimeError(f"aggregate offer {offer['id']} update failed")
        deactivated.append(int(offer["id"]))

    condition_updates = 0
    for op in plan["condition_updates"]:
        offer_id = int(op["before"]["id"])
        current = client.fetch_rows("promo_offer_master", OFFER_MASTER_SELECT, filters={"id": f"eq.{offer_id}"}, limit=1)
        if not current or not current[0].get("is_active"):
            continue
        verify_before(current[0], op["before"], f"offer {offer_id}")
        result = client.update_row("promo_offer_master", {"id": f"eq.{offer_id}", "updated_at": f"eq.{op['before']['updated_at']}"}, {"conditions": op["conditions"], "updated_at": now()})
        if len(result) != 1:
            raise RuntimeError(f"condition update failed for offer {offer_id}")
        condition_updates += 1

    item_updates = 0
    for op in plan["item_name_updates"]:
        item_id = int(op["before"]["offer_item_id"])
        current = client.fetch_rows("promo_offer_items", "offer_item_id,updated_at,item_name", filters={"offer_item_id": f"eq.{item_id}"}, limit=1)
        if not current:
            continue
        if current[0].get("updated_at") != op["before"].get("updated_at") or current[0].get("item_name") != op["before"].get("item_name"):
            raise RuntimeError(f"item {item_id} changed before apply")
        result = client.update_row("promo_offer_items", {"offer_item_id": f"eq.{item_id}", "updated_at": f"eq.{op['before']['updated_at']}"}, {"item_name": op["item_name"], "updated_at": now()})
        if len(result) != 1:
            raise RuntimeError(f"item update failed for {item_id}")
        item_updates += 1

    # Extraction state is set only after the Offer writes above.
    final_true = {236, 239, 289, 300, 303, 317, 318, 368, 396, 409, 410, 423, 424, 425, 426, 440, 452, 453, 483, 484, 486, 519, 657, 696, 697, 698, 700, 701, 702}
    final_false = {254, 319, 322, 323, 326, 333, 397, 416, 428, 434, 435, 449, 450, 489, 490, 656, 676, 677, 461, 462, 699}
    for pid in final_true | final_false:
        row = plans.get(pid)
        if not row:
            continue
        value = pid in final_true
        client.update_row("clinic_memberships", {"plan_id": f"eq.{pid}"}, {"is_offer_extracted": value, "updated_at": now()})
    glo2 = next((row for row in current_memberships(client) if norm(row["membership_name"]) == norm("Glo2 Society Membership") and int(row["business_id"]) == 3022), None)
    if glo2:
        client.update_row("clinic_memberships", {"plan_id": f"eq.{glo2['plan_id']}"}, {"is_offer_extracted": True, "is_ocr_required": False, "need_browser": False, "updated_at": now()})

    return {
        "applied_at": now(),
        "updated_memberships": updated_plans,
        "inserted_services": inserted_services,
        "inserted_plans": inserted_plans,
        "created_or_reactivated_offers": created_offers,
        "deactivated_aggregate_offers": deactivated,
        "condition_updates": condition_updates,
        "item_name_updates": item_updates,
        "review": plan["review"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--input", type=Path, default=ARTIFACT)
    args = parser.parse_args()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    client = load_supabase_client(ROOT)
    if not args.apply:
        plan = build_plan(client)
        args.input.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"mode": "dry_run", "artifact": str(args.input), "membership_updates": len(plan["membership_updates"]), "service_inserts": len(plan["service_inserts"]), "offer_specs": len(plan["offer_specs"]), "condition_updates": len(plan["condition_updates"]), "item_name_updates": len(plan["item_name_updates"]), "aggregate_deactivations": len(plan["aggregate_offer_deactivations"])}, ensure_ascii=False))
        return 0
    plan = json.loads(args.input.read_text(encoding="utf-8"))
    result = apply_plan(client, plan)
    output = ARTIFACT_DIR / "round2_apply_result.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": "applied", "result": str(output), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
