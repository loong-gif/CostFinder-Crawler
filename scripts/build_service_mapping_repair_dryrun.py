#!/usr/bin/env python3
"""Build a read-only repair plan for offers with unresolved service items.

The command never writes to Supabase. It produces one JSON artifact containing
item-level candidates and enough upstream evidence for a later reviewed apply.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.clinic_service_taxonomy import service_name_enum_hint
from utils.supabase_access import fetch_all_rows, load_supabase_client


GENERIC_SCOPE_NAMES = {
    "all service",
    "all services",
    "all treatment",
    "all treatments",
    "injectables",
    "wellness",
}

EXCLUDED_TAXONOMY_TERMS = (
    "sculptra",
    "skincare product",
    "chemical peel",
    "prf",
    "kybella",
    "body contouring",
    "ez gel",
    "prp",
    "xerf",
    "exosome",
    "thread lift",
)

ALIASES = {
    "bbl": "BBL Photofacial",
    "bbl photofacial": "BBL Photofacial",
    "skinpen": "SkinPen Microneedling",
    "moxi": "Moxi Laser",
    "diamondglow facial": "DiamondGlow",
    "full arms laser hair removal": "Laser Hair Removal",
}

PACKAGE_COMPONENT_PATTERNS = (
    (re.compile(r"\bbbl\b", re.I), "BBL Photofacial"),
    (re.compile(r"\bmoxi\b", re.I), "Moxi Laser"),
    (re.compile(r"\bhalo\b", re.I), "Halo Laser"),
    (re.compile(r"\bskinpen\b", re.I), "SkinPen Microneedling"),
    (re.compile(r"\bmicroneedling\b", re.I), "Microneedling"),
    (re.compile(r"\bjuv[ée]r?derm\b", re.I), "Juvéderm"),
    (re.compile(r"\bbotox\b", re.I), "Botox"),
    (re.compile(r"\bdysport\b", re.I), "Dysport"),
    (re.compile(r"\bxeomin\b", re.I), "Xeomin"),
    (re.compile(r"\brestylane\b", re.I), "Restylane"),
    (re.compile(r"\bradiesse\b", re.I), "Radiesse"),
)


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _canonical_name(value: Any) -> str | None:
    raw = str(value or "").strip()
    direct = service_name_enum_hint(raw)
    if direct:
        return direct
    return ALIASES.get(_fold(raw))


def _is_generic_scope(name: str) -> bool:
    return _fold(name) in GENERIC_SCOPE_NAMES


def _excluded_term(name: str) -> str | None:
    folded = _fold(name)
    for term in EXCLUDED_TAXONOMY_TERMS:
        if re.search(rf"\b{re.escape(_fold(term))}\b", folded):
            return term
    return None


def _is_package(name: str) -> bool:
    return bool(
        re.search(r"\b(?:package|combo|combined)\b", name, re.I)
        or re.search(r"\s(?:\+|&)\s", name)
        or re.search(r"\s+and\s+", name, re.I)
    )


def _package_components(name: str) -> list[str]:
    components: list[str] = []
    for pattern, canonical in PACKAGE_COMPONENT_PATTERNS:
        if pattern.search(name) and canonical not in components:
            components.append(canonical)
    return components


def _candidate_services(item: Mapping[str, Any], services: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    canonical = item.get("normalized_service_name")
    if not canonical:
        return []
    wanted = _fold(canonical)
    business_id = item.get("business_id")
    return [
        {
            "service_id": row.get("service_id"),
            "service_name": str(row.get("service_name") or ""),
            "service_area": row.get("service_area"),
            "source_url": row.get("source_url"),
        }
        for row in services
        if row.get("business_id") == business_id and _fold(row.get("service_name")) == wanted
    ]


def _evidence_status(item: Mapping[str, Any]) -> str:
    has_url = bool(str(item.get("source_url") or "").strip())
    has_raw = bool(str(item.get("offer_raw_text") or "").strip())
    if has_url and has_raw:
        return "source_url_and_offer_raw_text"
    if has_url:
        return "source_url_only"
    if has_raw:
        return "offer_raw_text_only"
    return "missing_source_evidence"


def classify_item(item: Mapping[str, Any], services: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Classify one unresolved item without inventing a service mapping."""
    name = str(item.get("item_name") or "").strip()
    canonical = _canonical_name(name)
    excluded = _excluded_term(name)
    result: dict[str, Any] = {
        "normalized_service_name": canonical,
        "existing_service_id": None,
        "existing_service_candidates": [],
        "service_area": None,
        "taxonomy_status": "current_taxonomy" if canonical else "unknown",
        "repair_action": "REVIEW",
        "review_reason": None,
        "evidence_status": _evidence_status(item),
        "atomic_service_names": [],
    }

    if _is_generic_scope(name):
        result.update(
            repair_action="EXPAND_GENERIC_SCOPE",
            taxonomy_status="generic_scope",
            review_reason="expand_against_business_service_catalogue",
        )
        return result

    if _is_package(name):
        components = _package_components(name)
        result["atomic_service_names"] = components
        if len(components) >= 2:
            result.update(
                repair_action="DECOMPOSE_PACKAGE",
                taxonomy_status="current_taxonomy" if not excluded else "mixed_taxonomy",
                review_reason=None if not excluded else "package_contains_taxonomy_excluded_component",
            )
        else:
            result.update(
                repair_action="REVIEW",
                taxonomy_status="unknown",
                review_reason="package_components_need_source_review",
            )
        return result

    if excluded:
        result.update(
            taxonomy_status="excluded_from_current_enum",
            repair_action="TAXONOMY_RESTORE_REVIEW",
            review_reason=f"service_excluded_from_current_taxonomy:{excluded}",
        )
        return result

    if not canonical:
        result.update(
            taxonomy_status="unknown",
            repair_action="REVIEW",
            review_reason="source_review_required_for_atomic_service",
        )
        return result

    candidates = _candidate_services({**item, "normalized_service_name": canonical}, services)
    result["existing_service_candidates"] = candidates
    if len(candidates) == 1:
        result.update(
            repair_action="LINK_EXISTING_SERVICE",
            existing_service_id=candidates[0]["service_id"],
            service_area=candidates[0]["service_area"],
        )
    elif len(candidates) > 1:
        result.update(
            repair_action="REVIEW",
            review_reason="multiple_existing_service_candidates",
        )
    elif result["evidence_status"] in {"source_url_and_offer_raw_text", "source_url_only"}:
        result.update(repair_action="CREATE_SERVICE_AND_LINK")
    else:
        result.update(
            repair_action="REVIEW",
            review_reason="missing_source_url_for_service_creation",
        )
    return result


def _parent_status(
    offer: Mapping[str, Any],
    promotions: Mapping[Any, Mapping[str, Any]],
    memberships: Mapping[Any, Mapping[str, Any]],
) -> str:
    promotion_id = offer.get("promotion_id")
    membership_plan_id = offer.get("membership_plan_id")
    if promotion_id is not None:
        promotion = promotions.get(promotion_id)
        if not promotion or promotion.get("business_id") != offer.get("business_id"):
            return "promotion_missing_or_business_mismatch"
        end_date = promotion.get("campaign_end_date")
        expired = bool(end_date and date.fromisoformat(str(end_date)[:10]) < date.today())
        if promotion.get("is_active") and not expired:
            return "promotion_active_unexpired"
        if expired:
            return "promotion_expired"
        return "promotion_inactive"
    if membership_plan_id is not None:
        membership = memberships.get(membership_plan_id)
        if membership and membership.get("business_id") == offer.get("business_id"):
            return "membership_business_match"
        return "membership_missing_or_business_mismatch"
    return "no_valid_upstream_fk"


def build_report(
    offers: Iterable[Mapping[str, Any]],
    services: Iterable[Mapping[str, Any]],
    promotions: Iterable[Mapping[str, Any]],
    memberships: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    offers = list(offers)
    services = list(services)
    promotion_map = {row.get("promotion_id"): row for row in promotions}
    membership_map = {row.get("plan_id"): row for row in memberships}
    rows: list[dict[str, Any]] = []
    offer_actions: defaultdict[Any, set[str]] = defaultdict(set)

    for offer in offers:
        items = offer.get("promo_offer_items") or []
        for raw_item in items:
            if raw_item.get("service_id") is not None:
                continue
            promotion = promotion_map.get(offer.get("promotion_id")) or {}
            membership = membership_map.get(offer.get("membership_plan_id")) or {}
            source_url = promotion.get("source_url") or membership.get("source_url")
            item = {
                "offer_id": offer.get("id"),
                "offer_item_id": raw_item.get("offer_item_id"),
                "business_id": offer.get("business_id"),
                "promotion_id": offer.get("promotion_id"),
                "membership_plan_id": offer.get("membership_plan_id"),
                "is_active": bool(offer.get("is_active")),
                "source_url": source_url,
                "offer_raw_text": offer.get("offer_raw_text"),
                "item_name": raw_item.get("item_name"),
                "regular_price": offer.get("regular_price"),
                "discount_price": offer.get("discount_price"),
                "price_model": offer.get("price_model"),
                "unit_price": raw_item.get("unit_price"),
                "delivered_units": raw_item.get("delivered_units"),
            }
            classification = classify_item(item, services)
            row = {**item, **classification, "parent_status": _parent_status(offer, promotion_map, membership_map)}
            rows.append(row)
            offer_actions[offer.get("id")].add(row["repair_action"])

    action_counts = Counter(row["repair_action"] for row in rows)
    active_rows = [row for row in rows if row["is_active"]]
    offer_classifications = []
    for offer_id, actions in offer_actions.items():
        offer_classifications.append(
            {
                "offer_id": offer_id,
                "repair_actions": sorted(actions),
                "classification": "MIXED" if len(actions) > 1 else next(iter(actions)),
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "summary": {
            "offer_count": len(offers),
            "unresolved_offer_count": len(offer_actions),
            "unresolved_item_count": len(rows),
            "active_unresolved_offer_count": len({row["offer_id"] for row in active_rows}),
            "active_unresolved_item_count": len(active_rows),
            "inactive_unresolved_offer_count": len({row["offer_id"] for row in rows if not row["is_active"]}),
            "by_repair_action": dict(sorted(action_counts.items())),
        },
        "offer_classifications": sorted(offer_classifications, key=lambda row: row["offer_id"]),
        "items": rows,
    }


def _load_live_report() -> dict[str, Any]:
    client = load_supabase_client(ROOT)
    offers = fetch_all_rows(
        client,
        "promo_offer_master",
        "id,business_id,promotion_id,membership_plan_id,is_active,offer_raw_text,regular_price,discount_price,price_model,is_package,promo_offer_items(offer_item_id,offer_id,item_name,service_id,delivered_units,unit_price)",
        page_size=500,
        order="id.asc",
    )
    services = fetch_all_rows(
        client,
        "clinic_services",
        "service_id,business_id,service_name,service_area,source_url",
        page_size=1000,
        order="service_id.asc",
    )
    promotions = fetch_all_rows(
        client,
        "clinic_promotions",
        "promotion_id,business_id,source_url,campaign_end_date,is_active",
        page_size=1000,
        order="promotion_id.asc",
    )
    memberships = fetch_all_rows(
        client,
        "clinic_memberships",
        "plan_id,business_id,source_url",
        page_size=1000,
        order="plan_id.asc",
    )
    return build_report(offers, services, promotions, memberships)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "outputs" / "service_mapping_repair",
        help="Directory for the generated JSON artifact",
    )
    args = parser.parse_args()
    report = _load_live_report()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir / f"service_mapping_repair_dryrun_{stamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
