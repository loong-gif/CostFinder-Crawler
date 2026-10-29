#!/usr/bin/env python3
"""Controlled, evidence-first application of the 2026-09-17 membership plan.

This is intentionally narrower than the historical full-table repair script.  It
freezes the live rows, applies only explicit source-backed changes, and uses
``updated_at`` as the optimistic-concurrency guard.  Run without ``--apply`` to
write the dry-run artifact; pass that artifact back with ``--apply`` to write.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.schema_contract import CLINIC_MEMBERSHIP_SELECT, OFFER_MASTER_SELECT
from utils.supabase_access import fetch_all_rows, load_supabase_client

ARTIFACT_DIR = ROOT / ".firecrawl" / "clinic_memberships_plan_20260917"
DEFAULT_ARTIFACT = ARTIFACT_DIR / "controlled_apply.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def dedupe_benefits(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = compact_text(text)
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def add_benefits(current: Iterable[Any], additions: Iterable[str]) -> List[str]:
    return dedupe_benefits([*current, *additions])


def remove_benefits(current: Iterable[Any], removals: Iterable[str]) -> List[str]:
    remove_keys = {compact_text(item) for item in removals}
    return dedupe_benefits(item for item in current if compact_text(item) not in remove_keys)


def find(rows: List[Dict[str, Any]], plan_id: int) -> Dict[str, Any]:
    for row in rows:
        if int(row["plan_id"]) == plan_id:
            return row
    raise RuntimeError(f"missing expected clinic_memberships row {plan_id}")


def desired_updates(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Return only fields supported by current source evidence."""
    updates: Dict[int, Dict[str, Any]] = {}

    current = {int(row["plan_id"]): row for row in rows}

    updates[236] = {
        "benefits": [
            "$89 monthly Franco Wallet treatment credit; credit does not expire while membership is active",
            "15% off every service",
            "Early access to seasonal promotions",
            "First access to surplus product at member-only pricing",
            "Priority booking",
            "Wallet credit applies to services and retail products",
            "Unused wallet credit is forfeited upon cancellation",
            "12-month commitment",
        ]
    }
    updates[254] = {
        "benefits": [
            "Free B12 injections every month for members",
            "$99 monthly Belle Âme Bank credit may be used immediately or banked for future visits",
            "Belle Âme Bank credit is non-refundable, non-transferable, and does not expire",
            "15% off all purchases",
            "20% off spend between $2,500 and $5,000",
            "25% off spend over $5,000",
            "12-month commitment",
        ]
    }
    updates[318] = {
        "benefits": add_benefits(
            remove_benefits(current[318]["benefits"], ["Joining gifts as listed on source page"]),
            [
                "Complimentary skincare product up to $100 value",
                "Complimentary skincare and treatment planning consultation",
            ],
        )
    }
    # The source currently supports the price and savings scope, but not the
    # plan's older rollover/fee wording.  Keep those unverified claims out.
    updates[319] = {
        "membership_price": 149.0,
        "benefits": [
            "$149 per month",
            "Save 10% to 30% on Fillers, Botox, PRF, Chemical Peels, Kybella, and Microneedling",
        ],
    }
    updates[322] = {
        "minimum_commitment_months": 6,
        "benefits": add_benefits(
            remove_benefits(current[322]["benefits"], ["One facial or skin rejuvenation treatment each month"]),
            [
                "One monthly treatment of choice: IPL, Bespoke Microneedle, ZO Stimulation Facial with Dermaplane, or RF Skin Tightening",
                "If a monthly treatment is missed, one treatment may roll over per 6-month period",
            ],
        ),
    }
    updates[323] = {"minimum_commitment_months": 6}
    updates[689] = {
        "minimum_commitment_months": 6,
        "benefits": add_benefits(
            current[689]["benefits"],
            ["If a monthly treatment is missed, one treatment may roll over per 6-month period"],
        ),
    }

    for plan_id in (346, 347, 348):
        # These rows contain the same filler benefit twice with capitalization
        # differences.  Keep the first evidence-backed instance and remove only
        # the duplicate through normalisation.
        updates[plan_id] = {"benefits": dedupe_benefits(current[plan_id]["benefits"])}

    updates[409] = {
        "benefits": add_benefits(
            current[409]["benefits"],
            ["Beauty Bank deposit: $99 monthly", "Beauty Bank balance can be used toward services and products"],
        )
    }
    updates[410] = {
        "benefits": add_benefits(
            current[410]["benefits"],
            ["Beauty Bank deposit: $199 monthly", "Beauty Bank balance can be used toward services and products"],
        )
    }

    club_fx = [
        "$99 monthly Beauty Bank",
        "15% off injectables",
        "BOTOX $10.99 per unit",
        "DYSPORT $3.15 per unit",
        "20% off lasers and devices",
        "20% off skincare",
        "Unlock savings for the Body-FX savings plan",
        "VIP events and raffles",
        "Free LED light therapy during birthday month",
        "$99 sign-up fee applies",
        "6-month commitment",
        "Cannot be combined with other specials",
    ]
    updates[411] = {"benefits": club_fx}
    updates[567] = {"benefits": club_fx}

    wallet = [
        "Monthly membership payment accumulates as Beauty Wallet credit",
        "Beauty Wallet credits can be applied to Botox and other treatments or purchases",
        "Beauty Wallet credits do not expire after cancellation",
    ]
    for plan_id in (422, 423, 424, 425, 426):
        updates[plan_id] = {
            "source_url": "https://nobhillaesthetics.com/pages/membership",
            "benefits": add_benefits(current[plan_id]["benefits"], wallet),
        }

    canonical_neurotoxin = "$10.50/unit neurotoxins"
    for plan_id in (465, 466, 469, 470):
        updates[plan_id] = {
            "benefits": add_benefits(
                remove_benefits(
                    current[plan_id]["benefits"],
                    ["$10.50 a unit for neurotoxins", "$10.50 per unit for neurotoxins"],
                ),
                [canonical_neurotoxin],
            )
        }

    return updates


def new_plans() -> List[Dict[str, Any]]:
    california_shared = [
        "Exclusive member promotions",
        "20% off skincare products and lashes when purchased at an appointment",
        "Earn Allē points",
    ]
    return [
        {
            "business_id": 3020,
            "membership_name": "TOX Membership",
            "membership_price": 134.0,
            "billing_period": "monthly",
            "minimum_commitment_months": None,
            "source_url": "https://californiacosmetics.com/products/botox-membership",
            "benefits": [
                "$134 per month",
                "One-time startup fee of $299",
                "Up to 4 Botox or Jeuveau treatments every 90 days",
                "Save up to $900 annually",
                "VIP discounts on additional units and treatments",
                *california_shared,
            ],
        },
        {
            "business_id": 3020,
            "membership_name": "TOX & More Membership",
            "membership_price": 234.0,
            "billing_period": "monthly",
            "minimum_commitment_months": None,
            "source_url": "https://californiacosmetics.com/products/botox-more-member",
            "benefits": [
                "$234 billed monthly",
                "One-time startup fee of $399",
                "Up to 4 Botox or Jeuveau treatments every 90 days",
                "JUVEDERM injections included",
                "Save up to $1,350 annually",
                "VIP discounts on additional units and treatments",
                *california_shared,
            ],
        },
        {
            "business_id": 3020,
            "membership_name": "Deluxe HydraFacial Membership",
            "membership_price": 209.0,
            "billing_period": "monthly",
            "minimum_commitment_months": None,
            "source_url": "https://californiacosmetics.com/products/hydrafacial-member",
            "benefits": [
                "$209 per month",
                "One HydraFacial treatment every month",
                "Two complimentary boosters per year",
                "Save up to $460 annually",
                "Discounts on additional HydraFacial treatments and SkinMedica products",
                "Member-only appointment times and exclusive perks",
                *california_shared,
            ],
        },
        {
            "business_id": 3020,
            "membership_name": "Skin Goals Membership",
            "membership_price": 299.0,
            "billing_period": "monthly",
            "minimum_commitment_months": None,
            "source_url": "https://californiacosmetics.com/products/skin-goals-member",
            "benefits": [
                "$299 billed monthly",
                "One-time startup fee of $750",
                "Four med-spa experiences",
                "Save up to $2,200 annually",
                "Treatment choices include PRP facials, HydraFacials, IPL Photofacials, and Pearl Fractional laser therapy",
                *california_shared,
            ],
        },
        {
            "business_id": 2995,
            "membership_name": "Deluxe Hydrafacial Membership",
            "membership_price": None,
            "billing_period": None,
            "minimum_commitment_months": 3,
            "source_url": "https://skinmaison.com/beauty-bank",
            "benefits": [
                "Signature HydraFacial with customized serum and LED light therapy (45 minutes)",
                "Tox $2 off per unit",
                "Injectables $100 off",
                "Skin Treatments 10% off",
                "Skincare Products 15% off",
                "Complimentary enhancement",
                "3-month commitment",
            ],
        },
        {
            "business_id": 3046,
            "membership_name": "Botox Membership",
            "membership_price": None,
            "billing_period": "monthly",
            "minimum_commitment_months": None,
            "source_url": "https://thefixclinic.com/memberships",
            "benefits": [
                "Choose a monthly investment of $150, $200, $250, or $300",
                "Monthly investment is banked for Botox, Dysport, Xeomin, or Daxxify treatments",
                "Member-only pricing: save $2 per unit",
                "Priority model call opportunities",
                "Free entry to Fix Clinic events",
            ],
        },
    ]


def relevant_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in CLINIC_MEMBERSHIP_SELECT.split(",")}


def relevant_offer(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("id", "membership_plan_id", "is_active", "updated_at", "offer_raw_text")
    return {key: row.get(key) for key in keys}


def values_match(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list) and isinstance(expected, list):
        return dedupe_benefits(actual) == dedupe_benefits(expected)
    return actual == expected


def verify_expected(row: Dict[str, Any], expected: Dict[str, Any], *, label: str) -> None:
    for key, value in expected.items():
        if not values_match(row.get(key), value):
            raise RuntimeError(f"concurrency conflict for {label}: {key} changed")


def version_filter(column: str, value: Any) -> str:
    return f"{column}=is.null" if value is None else f"{column}=eq.{value}"


def build_artifact(client: Any) -> Dict[str, Any]:
    ids = sorted({236, 254, 318, 319, 322, 323, 346, 347, 348, 409, 410, 411, 422, 423, 424, 425, 426, 465, 466, 469, 470, 567, 689})
    memberships = fetch_all_rows(
        client,
        "clinic_memberships",
        CLINIC_MEMBERSHIP_SELECT,
        filters={"plan_id": f"in.({','.join(str(value) for value in [*ids, 412])})"},
        order="plan_id.asc",
    )
    updates = desired_updates(memberships)
    target_rows = {int(row["plan_id"]): row for row in memberships}
    for plan_id in updates:
        if plan_id not in target_rows:
            raise RuntimeError(f"missing target plan {plan_id}")
    offers = fetch_all_rows(
        client,
        "promo_offer_master",
        OFFER_MASTER_SELECT,
        filters={"membership_plan_id": "eq.412"},
        order="id.asc",
    )
    update_ops = []
    for plan_id, payload in sorted(updates.items()):
        update_ops.append(
            {
                "plan_id": plan_id,
                "before": relevant_row(target_rows[plan_id]),
                "payload": payload,
            }
        )
    return {
        "created_at": utc_now(),
        "scope": "clinic_memberships_plan_20260917",
        "membership_updates": update_ops,
        "wrong_plan_412": relevant_row(target_rows[412]),
        "wrong_plan_412_offers": [relevant_offer(row) for row in offers],
        "new_plans": new_plans(),
        "manual_review": [
            {"scope": "the_med_spot", "plan_ids": [459, 460, 461, 462, 463, 464], "reason": "current source exposes package image but not a reliable tier-to-package mapping"},
            {"scope": "house_of_aesthetics", "reason": "current source was blocked by Cloudflare; Glo2 Society was not inserted"},
            {"scope": "downstream_offer_sync", "plan_ids": [318, 319, 322, 323, 409, 410, 411, 422, 423, 424, 425, 426, 465, 466, 469, 470, 567, 689], "reason": "membership text is updated; offer item/service mapping is not inferred where source evidence is not a priced service mapping"},
        ],
    }


def apply_artifact(client: Any, artifact: Dict[str, Any]) -> Dict[str, Any]:
    current_rows = fetch_all_rows(client, "clinic_memberships", CLINIC_MEMBERSHIP_SELECT, order="plan_id.asc")
    by_id = {int(row["plan_id"]): row for row in current_rows}
    applied_updates: List[int] = []
    for operation in artifact["membership_updates"]:
        plan_id = int(operation["plan_id"])
        row = by_id.get(plan_id)
        if row is None:
            raise RuntimeError(f"plan {plan_id} disappeared before apply")
        before = operation["before"]
        payload = dict(operation["payload"])
        if all(values_match(row.get(key), value) for key, value in payload.items()):
            # The previous attempt may have completed this row before a later
            # row failed.  Treat an already-applied desired payload as a safe
            # resume, while still rejecting any other drift.
            applied_updates.append(plan_id)
            continue
        verify_expected(row, before, label=f"plan {plan_id}")
        payload["updated_at"] = utc_now()
        returned = client.update_row(
            "clinic_memberships",
            {"plan_id": f"eq.{plan_id}", version_filter("updated_at", before["updated_at"]).split("=", 1)[0]: version_filter("updated_at", before["updated_at"]).split("=", 1)[1]},
            payload,
        )
        if len(returned) != 1:
            raise RuntimeError(f"plan {plan_id} update returned {len(returned)} rows")
        applied_updates.append(plan_id)

    old_offers = fetch_all_rows(
        client,
        "promo_offer_master",
        OFFER_MASTER_SELECT,
        filters={"membership_plan_id": "eq.412"},
        order="id.asc",
    )
    frozen_offers = {int(row["id"]): row for row in artifact["wrong_plan_412_offers"]}
    for row in old_offers:
        offer_id = int(row["id"])
        frozen = frozen_offers.get(offer_id)
        if frozen is None:
            raise RuntimeError(f"new offer {offer_id} appeared under wrong plan 412")
        verify_expected(row, frozen, label=f"offer {offer_id}")
        returned = client.update_row(
            "promo_offer_master",
            {"id": f"eq.{offer_id}", "updated_at": f"eq.{frozen['updated_at']}"},
            {"is_active": False, "membership_plan_id": None, "updated_at": utc_now()},
        )
        if len(returned) != 1:
            raise RuntimeError(f"offer {offer_id} update returned {len(returned)} rows")

    remaining_refs = fetch_all_rows(client, "promo_offer_master", "id", filters={"membership_plan_id": "eq.412"})
    if remaining_refs:
        raise RuntimeError("cannot remove plan 412 while offer foreign-key references remain")
    deleted = client.delete_rows("clinic_memberships", {"plan_id": "eq.412"})
    if len(deleted) != 1:
        raise RuntimeError(f"plan 412 delete returned {len(deleted)} rows")

    inserted: List[Dict[str, Any]] = []
    for plan in artifact["new_plans"]:
        existing = fetch_all_rows(
            client,
            "clinic_memberships",
            CLINIC_MEMBERSHIP_SELECT,
            filters={"business_id": f"eq.{plan['business_id']}"},
        )
        duplicate = [row for row in existing if compact_text(row.get("membership_name")) == compact_text(plan["membership_name"])]
        if duplicate:
            raise RuntimeError(f"new plan already exists: {plan['business_id']} / {plan['membership_name']}")
        payload = dict(plan)
        payload["created_at"] = utc_now()
        payload["updated_at"] = payload["created_at"]
        created = client.insert_rows("clinic_memberships", [payload])
        if len(created) != 1:
            raise RuntimeError(f"insert for {plan['membership_name']} returned {len(created)} rows")
        inserted.append({"plan_id": created[0].get("plan_id"), "membership_name": plan["membership_name"], "business_id": plan["business_id"]})

    qa_rows = fetch_all_rows(client, "clinic_memberships", CLINIC_MEMBERSHIP_SELECT, order="plan_id.asc")
    qa_offers = fetch_all_rows(client, "promo_offer_master", OFFER_MASTER_SELECT, order="id.asc")
    placeholders = []
    for row in qa_rows:
        for benefit in row.get("benefits") or []:
            if "image-only" in compact_text(benefit) or "as listed on source" in compact_text(benefit):
                placeholders.append({"plan_id": row["plan_id"], "benefit": benefit})
    return {
        "applied_at": utc_now(),
        "updated_plan_ids": applied_updates,
        "deleted_plan_ids": [412],
        "deactivated_and_unlinked_offer_ids": [row["id"] for row in old_offers],
        "inserted_plans": inserted,
        "qa": {
            "clinic_memberships_count": len(qa_rows),
            "offer_refs_to_deleted_412": [row["id"] for row in qa_offers if row.get("membership_plan_id") == 412],
            "placeholder_benefits": placeholders,
            "manual_review": artifact["manual_review"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--input", type=Path, default=DEFAULT_ARTIFACT)
    args = parser.parse_args()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    client = load_supabase_client(ROOT)
    if not args.apply:
        artifact = build_artifact(client)
        args.input.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"mode": "dry_run", "artifact": str(args.input), "updates": len(artifact["membership_updates"]), "offers_to_rehome": len(artifact["wrong_plan_412_offers"]), "inserts": len(artifact["new_plans"])}, ensure_ascii=False))
        return 0
    artifact = json.loads(args.input.read_text(encoding="utf-8"))
    result = apply_artifact(client, artifact)
    output = ARTIFACT_DIR / "apply_result.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": "applied", "result": str(output), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
