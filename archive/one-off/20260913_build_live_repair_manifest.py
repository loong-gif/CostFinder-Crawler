#!/usr/bin/env python3
"""Build an evidence-first repair manifest from the live QA reports."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.supabase_access import fetch_all_rows, load_supabase_client

PROMO_REPORT = ROOT / "clinic_promotions_live_qa.json"
MEMBERSHIP_REPORT = ROOT / "clinic_memberships_live_qa.json"
OUT = ROOT / ".firecrawl" / "live_repair_manifest_20260913.json"

PROMO_REPAIR_STATUSES = {
    "live_updated",
    "db_missing_content",
    "mixed",
    "db_extra_content",
    "redirected_changed",
}
MEMBERSHIP_REPAIR_STATUSES = {"db_missing_content", "live_updated", "db_extra_content"}

LIVE_BENEFITS: dict[int, list[str]] = {
    236: ["$89 back every month as Franco Wallet treatment credit that never expires", "15% off every service", "Early access to seasonal promotions", "First access to surplus product at member-only pricing", "Priority booking", "Wallet credit applies to services and retail products", "12-month commitment"],
    237: ["20% off products", "20% off all laser and body contouring", "$10.50 Botox unit", "$10.50 Dysport unit", "One customized facial, Level 1 chemical peel, free laser hair removal, or small tattoo removal per month", "Three free maintenance EmSculpt sessions per year", "12-month commitment"],
    238: ["One facial of choice monthly including Deluxe HydraFacial ($275 value)", "$11 per unit Botox, Xeomin, Dysport and Letybo", "10% off dermal fillers and Sculptra", "10% off laser, microneedling and treatment packages", "10% off medical-grade skincare", "Unused facial converts to $200 Skin Bank Package Credit when applied to a prepaid package of $1,000 or more", "12-month commitment"],
    239: ["$11/unit Botox & Dysport", "10% off Wellness Collective, injectables, skin treatments/packages, facials/peels, skincare/add-ons", "One complimentary B12 or B-Complex injection monthly"],
    240: ["Bank $149 each month for any service or product; does not expire", "20% off massages and facials", "$12/unit neurotoxin", "10% off dermal filler", "10% off products", "Unlimited salt room access", "$25 member referral credit", "Cancel anytime with 30 days notice", "No sign-up, cancellation or hidden fees", "Month-to-month payments", "Rollover unused dollars", "Transfer credits to friends or family"],
    241: ["15% off products", "15% off all laser and body contouring", "$11 Botox unit", "$10 Dysport unit", "One customized facial or Level 1 chemical peel per month", "12-month commitment"],
    242: ["$12.50/unit Botox", "$12.50/equivalent-dose Dysport", "5% off chemical peels, microneedling and PRF", "$99/month TRU ESSENTIALS membership"],
    244: ["10% off products", "10% off all laser treatments and body contouring", "$11.50 Botox unit", "$11.50 Dysport unit", "12-month commitment"],
    247: ["$11.50/unit Botox and Dysport", "$50 off each filler", "$50 off each syringe of biostimulator", "10% off skin-quality treatments including SkinVive", "Annual savings of $1,400+ based on usage", "$199/month TRU SIGNATURE membership"],
    250: ["15% off medical skincare products", "10% off Moxi and BBL single-price treatment", "10% off neurotoxins", "5% off dermal filler", "5% off chemical peels", "One complimentary treatment per month ($100 value)", "Named monthly complimentary options and quarterly wellness substitutions are displayed on the live membership page", "6-month commitment"],
    289: ["15% off all services and products from day one", "Exclusive member-only pricing on select treatments", "$50 birthday gift card", "Two guest passes per year", "$179 monthly deposit becomes credit for any service or product", "One free elite-selection service every quarter: laser hair removal, mini facial, dermaplane or B12 shot", "Tox as low as $9/unit", "3-month commitment"],
    326: ["Choose one treatment every month: facial, peel or LED", "Complimentary birthday facial", "20% off all services and skincare", "15% off treatment series purchases"],
    366: ["Two custom spray tans", "Choice of dermaplane or lip flip", "One small-area laser hair-removal treatment", "Beauty Bank funds are usable for facials, toxins and fillers, laser treatments, and advanced aesthetic treatments", "6-month commitment"],
    367: ["One Deluxe Hydrafacial or microneedling with dermaplane", "Two custom spray tans", "One small-area laser hair-removal treatment", "Beauty Bank funds are usable for facials, toxins and fillers, laser treatments, and advanced aesthetic treatments", "6-month commitment"],
    368: ["Beauty Bank funds are usable for facials, toxins and fillers, laser treatments, and advanced aesthetic treatments"],
    458: ["$9.50 Botox or Jeuveau", "10% off all syringes", "10% off all retail items", "6-month commitment"],
    485: ["20% off all products", "10% off all services during membership period", "BBL/IPL, Clear Acne Laser or Laser Genesis plus one microdermabrasion or chemical peel per month", "Membership add-on pricing: 1 area $199, 2 areas $299, 3 areas $399"],
    487: ["20% off all products", "10% off all services during membership period", "One facial, chemical peel or micro plus collagen mask per month", "Add-on light therapy", "Membership add-on pricing: Décolleté $49, Neck $39, Hands $39"],
    488: ["20% off all products", "10% off all services during membership period", "RF microneedling with growth factor application, one area per month", "Membership add-on pricing: 1 area $199, 2 areas $299, 3 areas $399"],
    512: ["Bank $199 each month for any service or product; does not expire", "Free B-12 shot per month", "Unlimited salt room access", "$11/unit neurotoxin", "20% off massages and facials", "10% off wellness services", "10% off single laser treatments", "10% off dermal filler", "10% off products", "$25 member referral credit", "Cancel anytime with 30 days notice", "No sign-up, cancellation or hidden fees", "Month-to-month payments", "Rollover unused dollars", "Transfer credits to friends or family"],
    519: ["$11/unit Botox & Dysport", "10% off Wellness Collective, injectables, skin treatments/packages, facials/peels, skincare/add-ons", "One complimentary B12 or B-Complex injection monthly", "One complimentary monthly treatment: Diamond GLO Deluxe (regular $300), Forma Facial (regular $300), or Lumecca IPL (regular $400)"],
    672: ["Bank 10 units of Botox, Dysport, or Xeomin per month", "10% off skincare products", "10% off fillers", "10% off all laser services", "10% off all skin services", "First access to exclusive events", "$10/unit for members", "12-month commitment"],
    676: ["One Diamond Glow facial treatment per month", "10% off skincare products", "10% off all laser services", "10% off all skin services", "First access to exclusive events", "Save $100/month", "12-month commitment"],
    677: ["Pick one service per month: Forma, IPL, hair removal or Ultra", "10% off skincare products", "10% off all laser services", "10% off all skin services", "First access to exclusive events", "Save $85-$230/month", "12-month commitment"],
}


def normalized(value: object) -> str:
    return str(value or "").strip().rstrip("/").casefold()


def membership_live_evidence(report: dict, source_url: str) -> dict:
    key = normalized(source_url)
    matches = [item for item in report["url_evidence"] if normalized(item.get("requested_url")) == key]
    if not matches:
        return {}
    return matches[0]


def rebuilt_membership_benefits(row: dict, evidence: dict, qa_row: dict) -> list[str] | None:
    if qa_row["qa_status"] != "db_missing_content" or evidence.get("confidence") != "high":
        return None
    return LIVE_BENEFITS.get(int(row["plan_id"]))


def main() -> None:
    promotions = json.loads(PROMO_REPORT.read_text(encoding="utf-8"))
    memberships = json.loads(MEMBERSHIP_REPORT.read_text(encoding="utf-8"))
    client = load_supabase_client()
    promo_rows = {row["promotion_id"]: row for row in fetch_all_rows(client, "clinic_promotions", "*")}
    membership_rows = {row["plan_id"]: row for row in fetch_all_rows(client, "clinic_memberships", "*")}

    promo_entries = []
    for item in promotions["records"]:
        if item.get("status") not in PROMO_REPAIR_STATUSES:
            continue
        current = promo_rows.get(item["record_id"], {})
        direct = item.get("confidence") == "high" and item.get("browser_accessible") is True
        promo_entries.append(
            {
                "record_type": "promotion",
                "record_id": item["record_id"],
                "source_url": item.get("source_url"),
                "final_url": item.get("final_url"),
                "qa_status": item.get("status"),
                "confidence": item.get("confidence"),
                "old_content": current.get("promotion_content") or item.get("db_content") or [],
                "old_updated_at": current.get("updated_at"),
                "live_content": item.get("live_content"),
                "proposed_content": None,
                "evidence_text": item.get("evidence_text"),
                "recommended_action": item.get("recommended_action"),
                "decision": "review",
                "reason": "full live promotion_content is not present in the QA report; do not overwrite with a summary",
                "direct_live_evidence": direct,
            }
        )

    membership_entries = []
    for item in memberships["record_evidence"]:
        if item.get("qa_status") not in MEMBERSHIP_REPAIR_STATUSES:
            continue
        current = membership_rows.get(item["record_id"], {})
        evidence = membership_live_evidence(memberships, item.get("source_url") or "")
        proposed = rebuilt_membership_benefits(current, evidence, item)
        direct = evidence.get("confidence") == "high" and bool(evidence.get("evidence_text"))
        membership_entries.append(
            {
                "record_type": "membership",
                "record_id": item["record_id"],
                "source_url": item.get("source_url"),
                "final_url": evidence.get("final_url"),
                "qa_status": item.get("qa_status"),
                "confidence": evidence.get("confidence"),
                "old_content": current.get("benefits") or [],
                "old_updated_at": current.get("updated_at"),
                "live_content": evidence.get("evidence_text"),
                "proposed_content": proposed,
                "evidence_text": evidence.get("evidence_text"),
                "recommended_action": item.get("recommended_action"),
                "decision": "ready" if proposed is not None and direct else "review",
                "reason": "direct high-confidence live benefit evidence" if proposed is not None and direct else "live evidence does not provide a complete safe replacement",
                "direct_live_evidence": direct,
            }
        )

    ready = [item for item in [*promo_entries, *membership_entries] if item["decision"] == "ready"]
    review = [item for item in [*promo_entries, *membership_entries] if item["decision"] == "review"]
    payload = {
        "manifest_type": "costfinder_live_repair_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only_source": True,
        "source_reports": [str(PROMO_REPORT), str(MEMBERSHIP_REPORT)],
        "scope": {"promotion_candidates": len(promo_entries), "membership_candidates": len(membership_entries), "total_candidates": len(promo_entries) + len(membership_entries)},
        "decision_counts": {"ready": len(ready), "review": len(review)},
        "write_policy": "Only ready entries may be applied after a fresh snapshot/version check; review entries remain unchanged.",
        "entries": [*promo_entries, *membership_entries],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(OUT), "scope": payload["scope"], "decision_counts": payload["decision_counts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
