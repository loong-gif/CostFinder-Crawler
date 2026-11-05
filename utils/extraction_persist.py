"""Centralized extraction routing + persistence for clinic tables."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any, Literal, Mapping, Optional

from utils.clinic_promotions_db import upsert_promotion
from utils.clinic_service_extraction import (
    attach_service_ids_to_items,
    is_promo_offer,
    offer_to_clinic_service_item,
    pick_best_service_items,
    upsert_extracted_service,
)
from utils.offer_extraction_llm import canonicalize_service_name
from utils.offer_evidence_validator import validate_offer_evidence
from utils.offer_fingerprint import compute_offer_fingerprint, normalize_offer_raw_text
from utils.offer_scope_filter import exclude_reason, should_exclude_from_offer_master
from utils.promo_offer_items_db import is_blanket_offer_text, offer_item_semantics, upsert_offer_items
from utils.membership_duplicate_check import find_matching_membership_offer
from utils.recent_raw_extraction import (
    build_promotion_content,
    expand_promotion_content,
    validate_membership,
    validate_promotion,
    validate_service,
)
from utils.rendered_visibility_gate import filter_visible_promotion_content
from utils.schema_contract import TABLE_CLINIC_MEMBERSHIPS, TABLE_CLINIC_PROMOTIONS, TABLE_PROMO_OFFER_MASTER
from utils.service_price_guard import derive_offer_item_pricing
from utils.offer_purchase_semantics import (
    canonical_offer_semantics, new_customer_tier_merge_patch, normalize_purchase_offers,
    offer_service_tier_identity, offer_update_identity, service_supported_by_source, usable_unit_price,
)

RouteKind = Literal["service", "promo", "quarantine"]


def ensure_monitor_event_id(event: dict[str, Any]) -> dict[str, Any]:
    """Fill monitor_event_id when missing so DB unique index can dedupe retries."""
    if event.get("monitor_event_id"):
        return event
    crawl_run_id = event.get("crawl_run_id") or "unknown"
    segment_id = event.get("target_offer_id") or event.get("event_index") or "0"
    diff_type = event.get("diff_type") or "changed"
    event["monitor_event_id"] = f"{crawl_run_id}:{segment_id}:{diff_type}"
    return event


def _positive(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def route_offer(offer: dict[str, Any]) -> RouteKind:
    if is_blanket_offer_text(str(offer.get("offer_raw_text") or "")) and is_promo_offer(offer):
        return "promo"
    if should_exclude_from_offer_master(offer):
        return "quarantine"
    if is_promo_offer(offer):
        return "promo"
    if offer_to_clinic_service_item(offer) is not None:
        return "service"
    return "quarantine"


def infer_price_model(offer: dict[str, Any]) -> str:
    raw = str(offer.get("offer_raw_text") or "").lower()
    if re.search(r"\b(?:starting\s+at|starts?\s+(?:at|from)|from)\s+\$", raw):
        return "from"
    # These signals describe the price semantics, not merely the presence of
    # a quantity.  A phrase such as "2 syringes for $900" remains a total.
    if re.search(
        r"(?:(?:/|per)\s*|a\s+)(?:u(?:nit)?s?|syringe|syringes|vial|vials|session|sessions|treatment|treatments)\b",
        raw,
    ):
        return "per_unit"
    if re.search(r"\b(?:starting\s+at|starts?\s+(?:at|from)|from)\s+\$", raw):
        return "from"
    return "total"


def build_master_from_offer(
    offer: dict[str, Any],
    *,
    business_id: int,
    promotion_id: int,
    source_url: str,
    membership_plan_id: Optional[int] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not is_promo_offer(offer):
        raise ValueError(
            "promo_offer_master requires discount_price, discount_percent, or discount_amount"
        )
    raw_conditions = offer.get("conditions")
    if not isinstance(raw_conditions, list):
        raise ValueError("promo_offer_master requires verified conditions (use [] when none apply)")
    conditions: list[str] = []
    for condition in raw_conditions:
        if isinstance(condition, str):
            requirement = condition.strip()
        elif isinstance(condition, Mapping):
            # The extraction schema carries evidence until the shared gate
            # validates it; storage keeps only the requirement text.
            requirement_value = condition.get("requirement")
            evidence_value = condition.get("evidence")
            if not isinstance(requirement_value, str) or not isinstance(evidence_value, str) or not evidence_value.strip():
                raise ValueError("promo offer conditions require non-empty strings")
            requirement = requirement_value.strip()
        else:
            raise ValueError("promo offer conditions must be non-empty strings")
        if not requirement:
            raise ValueError("promo offer conditions require non-empty strings")
        conditions.append(requirement)
    items_in = offer.get("items") or []
    items_in = derive_offer_item_pricing(offer, evidence=str(offer.get("offer_raw_text") or ""))
    first = items_in[0] if items_in else {}
    is_pkg = offer.get("is_package") is True or offer.get("purchase_type") == "bundle"
    sibling_count = len(items_in)
    master_scope = str(offer.get("offer_raw_text") or "")
    if sibling_count > 1 or is_pkg:
        master_scope = str(first.get("item_name") or first.get("service_name") or master_scope)
    service_name = canonicalize_service_name(
        first.get("service_name"),
        first.get("service_area"),
        master_scope if sibling_count <= 1 and not is_pkg else first.get("item_name") or first.get("service_name"),
    ) or "Others"
    master: dict[str, Any] = {
        "business_id": business_id,
        "promotion_id": promotion_id,
        "is_active": True,
        "is_new_customer_required": offer.get("is_new_customer_required"),
        "is_membership_required": offer.get("is_membership_required"),
        "conditions": conditions,
        "offer_raw_text": str(offer.get("offer_raw_text") or service_name)[:4000],
        "price_model": infer_price_model(offer),
    }
    if offer.get("regular_price") is not None:
        master["regular_price"] = float(offer["regular_price"])
    if offer.get("discount_price") is not None:
        master["discount_price"] = float(offer["discount_price"])
    if master["price_model"] == "per_unit" and master.get("discount_price") is None:
        item_prices = [
            round(float(item["unit_price"]), 4)
            for item in items_in
            if item.get("unit_price") is not None
        ]
        if item_prices and len(item_prices) == len(items_in) and all(
            abs(price - item_prices[0]) <= 0.011 for price in item_prices
        ):
            master["discount_price"] = item_prices[0]
    if offer.get("discount_percent") is not None:
        master["discount_percent"] = float(offer["discount_percent"])
    if offer.get("discount_amount") is not None:
        master["discount_amount"] = float(offer["discount_amount"])
    if membership_plan_id is not None and offer.get("is_membership_required"):
        master["membership_plan_id"] = membership_plan_id
        master["promotion_id"] = None
    unit_type = str(first.get("unit_type") or "")
    raw_text = str(offer.get("offer_raw_text") or "")
    master["offer_fingerprint"] = compute_offer_fingerprint(
        source_url=source_url,
        service_name=service_name,
        unit_type=unit_type,
        regular_price=master.get("regular_price"),
        discount_price=master.get("discount_price"),
        offer_raw_text=raw_text,
        business_id=business_id,
        promotion_id=master.get("promotion_id"),
        membership_plan_id=master.get("membership_plan_id"),
        discount_percent=master.get("discount_percent"),
        discount_amount=master.get("discount_amount"),
    )
    item_rows: list[dict[str, Any]] = []
    for raw in items_in:
        source_item_name = str(
            raw.get("item_name")
            or raw.get("display_service_name")
            or raw.get("service_name")
            or "Offer item"
        ).strip()
        item_scope = raw_text
        if sibling_count > 1 or is_pkg:
            item_scope = str(raw.get("item_name") or raw.get("service_name") or "")
        name = canonicalize_service_name(
            raw.get("service_name"),
            raw.get("service_area"),
            item_scope,
        ) or str(raw.get("service_name") or "Offer item")
        # Keep the source-facing title separate from the canonical name used
        # for service matching. Unknown dictionary values must remain visible
        # instead of being rewritten to "Others".
        item: dict[str, Any] = {"item_name": source_item_name, "service_name": name}
        if raw.get("quantity") is not None:
            item["quantity"] = raw["quantity"]
        if raw.get("unit_price") is not None:
            item["unit_price"] = raw["unit_price"]
        item_rows.append(item)
    if not item_rows:
        item_rows = [{"service_name": service_name}]
    if is_blanket_offer_text(raw_text):
        item_rows = [{"item_name": raw_text}]
    return master, item_rows


def persist_service_item(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
) -> dict[str, Any]:
    return upsert_extracted_service(
        client,
        business_id=business_id,
        item=item,
        source_url=source_url,
        evidence=evidence,
    )


def persist_membership_item(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
    rendered_visibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from utils.membership_evidence import prepare_membership_write
    decision = prepare_membership_write(item, evidence, rendered_visibility)
    if not decision["accepted"]:
        return {"accepted": False, "reason": decision["reason"], "action": "skipped"}
    row = {
        "business_id": business_id,
        "membership_name": str(item.get("membership_name") or "").strip(),
        "membership_price": decision["membership_price"],
        "billing_period": item.get("billing_period"),
        "minimum_commitment_months": item.get("minimum_commitment_months"),
        "benefits": decision["benefits"],
        "source_url": str(source_url or "").strip().rstrip("/"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    matches = client.fetch_rows(TABLE_CLINIC_MEMBERSHIPS,
        "plan_id,business_id,membership_name,membership_price,benefits,billing_period,minimum_commitment_months,is_offer_extracted,source_url,updated_at",
        filters={"business_id": f"eq.{business_id}", "source_url": f"eq.{row['source_url']}"})
    matches = [old for old in matches if str(old.get("membership_name") or "").strip().casefold() == row["membership_name"].casefold()]
    if len(matches) > 1:
        return {"accepted": False, "reason": "ambiguous_membership_identity", "action": "quarantined"}
    # is_offer_extracted 只在新行或权益变化时置为 false（与 page_extraction.MEMBERSHIP_UPSERT 同一规则），
    # 之后由 extract-offers / link-services 在权益转成 offer 且服务映射完整后置为 true。
    if matches:
        old = matches[0]
        if decision["benefits"] is None:
            row.pop("benefits")
        elif decision["benefits"] != old.get("benefits"):
            row["is_offer_extracted"] = False
        if all(old.get(key) == value for key, value in row.items() if key != "updated_at"):
            return {"accepted": True, "action": "unchanged", "plan_id": int(old["plan_id"])}
        filters = {"plan_id": f"eq.{old['plan_id']}"}
        if old.get("updated_at"):
            filters["updated_at"] = f"eq.{old['updated_at']}"
        inserted = client.update_row(TABLE_CLINIC_MEMBERSHIPS, filters, row)
        if len(inserted) != 1:
            return {"accepted": False, "reason": "membership_write_conflict", "action": "quarantined"}
        action = "updated"
    else:
        row["is_offer_extracted"] = False
        inserted = client.insert_rows(TABLE_CLINIC_MEMBERSHIPS, [row])
        action = "inserted"
    plan_id = int(inserted[0]["plan_id"]) if action == "inserted" else int(matches[0]["plan_id"])
    actual = client.fetch_rows(TABLE_CLINIC_MEMBERSHIPS, ",".join(row),
                              filters={"plan_id": f"eq.{plan_id}"}, limit=1)
    if len(actual) != 1 or any(actual[0].get(key) != value for key, value in row.items() if key != "updated_at"):
        return {"accepted": False, "reason": "membership_readback_mismatch", "action": "quarantined", "plan_id": plan_id}
    return {
        "accepted": True,
        "action": action,
        "plan_id": plan_id,
    }


def persist_promotion_item(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
    rendered_visibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expanded = dict(item)
    expanded["promotion_content"] = build_promotion_content(item, evidence)
    visibility = filter_visible_promotion_content(
        expanded["promotion_content"], rendered_visibility
    )
    if not visibility.accepted:
        return {"accepted": False, "reason": visibility.reason, "action": "skipped"}
    expanded["promotion_content"] = list(visibility.content)
    decision = validate_promotion(expanded, evidence)
    if not decision.accepted:
        return {"accepted": False, "reason": decision.reason, "action": "skipped"}
    promotion_id = upsert_promotion(client, business_id=business_id, source_url=source_url)
    payload = {
        "promotion_title": str(expanded.get("promotion_title") or "Promotion"),
        "promotion_content": expanded.get("promotion_content") or [],
        "campaign_start_date": expanded.get("campaign_start_date"),
        "campaign_end_date": expanded.get("campaign_end_date"),
        "is_active": True,
    }
    current = client.fetch_rows(
        TABLE_CLINIC_PROMOTIONS,
        ",".join(payload),
        filters={"promotion_id": f"eq.{promotion_id}"},
        limit=1,
    )
    if current and all(current[0].get(key) == value for key, value in payload.items()):
        return {"accepted": True, "action": "unchanged", "promotion_id": int(promotion_id)}
    client.update_row(
        TABLE_CLINIC_PROMOTIONS,
        {"promotion_id": f"eq.{promotion_id}"},
        {**payload, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return {"accepted": True, "action": "upserted", "promotion_id": int(promotion_id)}


def persist_promo_offer(
    client: Any,
    *,
    business_id: int,
    promotion_id: int,
    source_url: str,
    offer: dict[str, Any],
    evidence: str,
    membership_plan_id: Optional[int] = None,
    seen_fingerprints: set[str] | None = None,
    _skip_purchase_normalize: bool = False,
    allow_unmapped_items: bool = False,
    trust_persisted_offer: bool = False,
) -> dict[str, Any]:
    if not _skip_purchase_normalize:
        normalized, purchase_reviews = normalize_purchase_offers([offer], evidence)
        rejected = [review for review in purchase_reviews if review.get("reason") != "generic_offer_scope"]
        if rejected:
            return {"accepted": False, "reason": rejected[0]["reason"], "action": "quarantined"}
        if len(normalized) > 1:
            writes = [
                persist_promo_offer(
                    client,
                    business_id=business_id,
                    promotion_id=promotion_id,
                    source_url=source_url,
                    offer=branch,
                    evidence=evidence,
                    membership_plan_id=membership_plan_id,
                    seen_fingerprints=seen_fingerprints,
                    _skip_purchase_normalize=True,
                    allow_unmapped_items=allow_unmapped_items,
                    trust_persisted_offer=trust_persisted_offer,
                )
                for branch in normalized
            ]
            accepted = [write for write in writes if write.get("accepted")]
            if len(accepted) == len(writes):
                return {"accepted": True, "action": "inserted", "split_writes": writes, "promos": len(accepted)}
            return {
                "accepted": False,
                "reason": next((w.get("reason") for w in writes if not w.get("accepted")), "purchase_split_partial"),
                "action": "quarantined",
                "split_writes": writes,
            }
        offer = normalized[0] if normalized else offer
    if trust_persisted_offer:
        from utils.offer_evidence_validator import OfferEvidenceDecision
        decision = OfferEvidenceDecision(True, "", offer, ())
    else:
        decision = validate_offer_evidence(
            offer,
            evidence,
        )
    if not decision.accepted:
        return {
            "accepted": False,
            "reason": decision.reason,
            "action": "quarantined",
            "evidence_issues": [issue.as_dict() for issue in decision.issues],
        }
    validated_offer = decision.offer
    if route_offer(validated_offer) != "promo":
        return {
            "accepted": False,
            "reason": exclude_reason(validated_offer) or "not_promo",
            "action": "skipped",
            "evidence_issues": [issue.as_dict() for issue in decision.issues],
        }

    # Validate ownership and the explicit promotion lifecycle flag before
    # writing anything. A source page date is not lifecycle evidence because
    # current-specials URLs can roll from one campaign to the next.
    promotion_rows = client.fetch_rows(
        TABLE_CLINIC_PROMOTIONS,
        "promotion_id,business_id,campaign_end_date,is_active",
        filters={"promotion_id": f"eq.{promotion_id}"},
        limit=1,
    )
    if promotion_rows:
        promotion = promotion_rows[0]
        if promotion.get("business_id") is not None and int(promotion["business_id"]) != int(business_id):
            return {"accepted": False, "reason": "promotion_business_mismatch", "action": "quarantined"}
        if promotion.get("is_active") is False:
            return {"accepted": False, "reason": "promotion_inactive", "action": "quarantined"}
    if membership_plan_id is not None:
        membership_rows = client.fetch_rows(
            TABLE_CLINIC_MEMBERSHIPS,
            "plan_id,business_id",
            filters={"plan_id": f"eq.{membership_plan_id}"},
            limit=1,
        )
        if membership_rows and membership_rows[0].get("business_id") is not None and int(membership_rows[0]["business_id"]) != int(business_id):
            return {"accepted": False, "reason": "membership_business_mismatch", "action": "quarantined"}
    master, items = build_master_from_offer(
        validated_offer,
        business_id=business_id,
        promotion_id=promotion_id,
        source_url=source_url,
        membership_plan_id=membership_plan_id,
    )
    blanket = is_blanket_offer_text(str(master.get("offer_raw_text") or ""))
    linked_items = items if blanket else attach_service_ids_to_items(
        client,
        business_id=business_id,
        items=items,
        fallback_text=str(validated_offer.get("offer_raw_text") or ""),
        is_package=validated_offer.get("is_package") is True or validated_offer.get("purchase_type") == "bundle",
    )
    # Only explicit blanket wording can remain outside the service catalogue.
    if not allow_unmapped_items and not blanket and any(item.get("service_id") is None for item in linked_items):
        return {
            "accepted": False,
            "reason": "unmapped_service_item",
            "action": "quarantined",
            "items": [
                {
                    "service_name": item.get("service_name"),
                    "item_name": item.get("item_name"),
                }
                for item in linked_items
                if item.get("service_id") is None
            ],
        }
    master["is_package"] = not blanket and (
        validated_offer.get("is_package") is True or validated_offer.get("purchase_type") == "bundle"
    )
    if len(linked_items) > 1 and not master["is_package"]:
        return {"accepted": False, "reason": "unresolved_purchase_composition", "action": "quarantined"}
    if master["is_package"]:
        linked_items = [dict(item, unit_price=None) for item in linked_items]
    elif master["price_model"] == "from":
        linked_items = [dict(item, unit_price=None) for item in linked_items]
    if not master["is_package"] and master.get("price_model") == "per_unit":
        master_discount_price = _positive(master.get("discount_price"))
        if master_discount_price is None:
            return {
                "accepted": False,
                "reason": "per_unit_price_incomplete",
                "action": "quarantined",
            }
        if any(
            _positive(item.get("unit_price")) is None
            or abs(float(item["unit_price"]) - master_discount_price) > 0.011
            for item in linked_items
        ):
            return {
                "accepted": False,
                "reason": "per_unit_item_price_incomplete",
                "action": "quarantined",
            }
    service_ids = [item.get("service_id") for item in linked_items if item.get("service_id") is not None]
    if service_ids:
        services = client.fetch_rows(
            "clinic_services",
            "service_id,business_id,service_name",
            filters={"service_id": f"in.({','.join(str(int(value)) for value in service_ids)})"},
            limit=len(service_ids),
        )
        if any(int(row["business_id"]) != int(business_id) for row in services if row.get("business_id") is not None):
            return {"accepted": False, "reason": "service_business_mismatch", "action": "quarantined"}
        by_id = {int(row["service_id"]): row for row in services if row.get("service_id") is not None}
        for item in linked_items:
            sid = item.get("service_id")
            if sid is None:
                continue
            row = by_id.get(int(sid))
            if not row or not row.get("service_name"):
                continue
            scope = str(item.get("item_name") or item.get("service_name") or master["offer_raw_text"])
            if not service_supported_by_source(row["service_name"], scope):
                return {"accepted": False, "reason": "service_not_named_in_source", "action": "quarantined"}
    # Live SQL computes fingerprints from items and prices. Compare source
    # identity and semantics directly instead of trusting a Python fingerprint.
    existing_rows = client.fetch_rows(
        TABLE_PROMO_OFFER_MASTER,
        "id,business_id,promotion_id,membership_plan_id,offer_raw_text,is_active,updated_at,"
        "regular_price,discount_price,discount_percent,discount_amount,price_model,is_package,"
        "is_new_customer_required,is_membership_required,conditions,"
        "promo_offer_items(offer_item_id,service_id,item_name,quantity:delivered_units,unit_price,updated_at)",
        filters={
            "business_id": f"eq.{business_id}",
            "promotion_id": f"eq.{master['promotion_id']}"
            if master.get("promotion_id") is not None else "is.null",
            "membership_plan_id": f"eq.{master['membership_plan_id']}"
            if master.get("membership_plan_id") is not None else "is.null",
        },
    )
    full_matches = [row for row in existing_rows if canonical_offer_semantics(row, row.get("promo_offer_items") or [])
                    == canonical_offer_semantics(master, linked_items)]
    if full_matches:
        existing = min(full_matches, key=lambda row: (not bool(row.get("is_active")), int(row["id"])))
        return {"accepted": False, "reason": "unchanged_offer" if existing.get("is_active") else "existing_inactive_offer",
                "action": "skipped", "offer_id": int(existing["id"])}
    matches = [row for row in existing_rows if offer_update_identity(row, row.get("promo_offer_items") or [])
               == offer_update_identity(master, linked_items)]
    same_raw = [row for row in existing_rows if normalize_offer_raw_text(row.get("offer_raw_text"))
                == normalize_offer_raw_text(master.get("offer_raw_text"))]
    if same_raw and not any(row.get("is_active") for row in same_raw):
        return {"accepted": False, "reason": "existing_inactive_offer", "action": "skipped", "offer_id": min(row["id"] for row in same_raw)}
    # Identical quoted copy is a safe update identity only with equal qualifications.
    if not matches:
        matches = [row for row in existing_rows if
            normalize_offer_raw_text(row.get("offer_raw_text")) == normalize_offer_raw_text(master.get("offer_raw_text"))
            and all(row.get(k) == master.get(k) for k in ("is_membership_required", "is_new_customer_required", "conditions"))]
    tier_key = offer_service_tier_identity(master, linked_items)
    if not matches:
        tier_matches = [row for row in existing_rows if row.get("is_active")
                        and offer_service_tier_identity(row, row.get("promo_offer_items") or []) == tier_key]
        if len(tier_matches) == 1:
            matches = tier_matches
        elif len(tier_matches) > 1:
            return {"accepted": False, "reason": "ambiguous_offer_identity", "action": "quarantined"}
    if len(matches) > 1:
        return {"accepted": False, "reason": "ambiguous_offer_identity", "action": "quarantined"}
    if matches:
        existing = matches[0]
        offer_id = int(existing["id"])
        if not existing.get("is_active"):
            return {"accepted": False, "reason": "existing_inactive_offer", "action": "skipped", "offer_id": offer_id}
        existing_items = existing.get("promo_offer_items") or []
        # An extractor's generic mapping label must not erase an already
        # scraped commercial title on the same service relation.
        old_names = {item.get("service_id"): item.get("item_name") for item in existing_items}
        linked_items = [
            dict(item, item_name=old_names[item.get("service_id")])
            if item.get("service_id") is not None
            and item.get("item_name") == item.get("service_name")
            and old_names.get(item.get("service_id")) else item
            for item in linked_items
        ]
        fields = ("regular_price", "discount_price", "discount_percent", "discount_amount",
                  "price_model", "is_package", "is_new_customer_required", "is_membership_required", "conditions")
        tier_patch = new_customer_tier_merge_patch(existing, master)
        if tier_patch is not None:
            patch = {field: tier_patch[field] for field in fields if field in tier_patch
                     and existing.get(field) != tier_patch[field]}
        else:
            patch = {field: master.get(field) for field in fields if existing.get(field) != master.get(field)}
        # SQL retains structural comparisons when the source states both
        # prices. They are QA arithmetic, not additional extracted facts.
        if master.get("regular_price") is not None and master.get("discount_price") is not None:
            regular = Decimal(str(master["regular_price"]))
            discount = Decimal(str(master["discount_price"]))
            if regular > 0 and 0 <= discount < regular:
                arithmetic = {
                    "discount_amount": (regular - discount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    "discount_percent": ((regular - discount) * 100 / regular).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                }
                patch = {field: value for field, value in patch.items() if not (
                    field in arithmetic and master.get(field) is None
                    and existing.get(field) is not None
                    and Decimal(str(existing[field])) == arithmetic[field]
                )}
        items_changed = offer_item_semantics(existing_items) != offer_item_semantics(linked_items)
        if not patch and not items_changed:
            return {"accepted": False, "reason": "unchanged_offer", "action": "skipped", "offer_id": offer_id}
        filters = {"id": f"eq.{offer_id}"}
        if existing.get("updated_at"):
            filters["updated_at"] = f"eq.{existing['updated_at']}"
        changed = client.update_row(TABLE_PROMO_OFFER_MASTER, filters,
                          {**patch, "offer_raw_text": master["offer_raw_text"], "is_active": False,
                           "updated_at": datetime.now(timezone.utc).isoformat()})
        if len(changed) != 1:
            return {"accepted": False, "reason": "offer_write_conflict", "action": "quarantined"}
        upsert_offer_items(client, offer_id, linked_items)
        activate_filters = {"id": f"eq.{offer_id}"}
        if changed[0].get("updated_at"):
            activate_filters["updated_at"] = f"eq.{changed[0]['updated_at']}"
        activated = client.update_row(TABLE_PROMO_OFFER_MASTER, activate_filters,
                          {"is_active": True, "updated_at": datetime.now(timezone.utc).isoformat()})
        if len(activated) != 1:
            return {"accepted": False, "reason": "offer_activation_conflict", "action": "quarantined"}
        if tier_patch is not None:
            for row in existing_rows:
                if int(row["id"]) == offer_id or not row.get("is_active"):
                    continue
                if offer_service_tier_identity(row, row.get("promo_offer_items") or []) != tier_key:
                    continue
                client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{row['id']}", "is_active": "is.true"},
                                  {"is_active": False, "updated_at": datetime.now(timezone.utc).isoformat()})
        expected_master = {**existing, **master, **(tier_patch or {})}
        verified = _verify_offer_write(client, offer_id, expected_master, linked_items)
        if not verified:
            return {"accepted": False, "reason": "offer_readback_mismatch", "action": "quarantined", "offer_id": offer_id}
        return {"accepted": True, "action": "updated", "offer_id": offer_id, "items": len(linked_items)}
    membership_match = find_matching_membership_offer(
        client,
        business_id=business_id,
        candidate_master=master,
        candidate_items=linked_items,
    )
    if membership_match is not None:
        return {
            "accepted": False,
            "reason": "membership_duplicate",
            "action": "skipped",
            "membership_offer_id": membership_match.get("id"),
            "membership_plan_id": membership_match.get("membership_plan_id"),
        }
    fp = str(master.get("offer_fingerprint") or "")
    # Build the relation first. The database activation guard intentionally
    # keeps a master inactive until its mapped items are present.
    master["is_active"] = False
    inserted = client.insert_rows(TABLE_PROMO_OFFER_MASTER, [master])
    offer_id = int(inserted[0]["id"])
    upsert_offer_items(client, offer_id, linked_items)
    client.update_row(
        TABLE_PROMO_OFFER_MASTER,
        {"id": f"eq.{offer_id}"},
        {"is_active": True, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    master["is_active"] = True
    if not _verify_offer_write(client, offer_id, master, linked_items):
        return {"accepted": False, "reason": "offer_readback_mismatch", "action": "quarantined", "offer_id": offer_id}
    if seen_fingerprints is not None and fp:
        seen_fingerprints.add(fp)
    return {
        "accepted": True,
        "action": "inserted",
        "offer_id": offer_id,
        "items": len(linked_items),
        "offer_fingerprint": fp,
        "evidence_issues": [issue.as_dict() for issue in decision.issues],
    }


def _verify_offer_write(client: Any, offer_id: int, master: dict, items: list[dict]) -> bool:
    rows = client.fetch_rows(TABLE_PROMO_OFFER_MASTER,
        "id,business_id,promotion_id,membership_plan_id,offer_raw_text,is_active,regular_price,discount_price,"
        "discount_amount,discount_percent,price_model,is_package,is_membership_required,is_new_customer_required,conditions,"
        "promo_offer_items(offer_item_id,service_id,item_name,quantity:delivered_units,unit_price)",
        filters={"id": f"eq.{offer_id}"}, limit=1)
    if len(rows) != 1 or rows[0].get("is_active") is not True:
        return False
    expected = dict(master)
    # Trigger arithmetic is permitted only for the source-stated comparable pair.
    regular, discount = expected.get("regular_price"), expected.get("discount_price")
    if regular is not None and discount is not None and regular > discount:
        for field in ("discount_amount", "discount_percent"):
            if expected.get(field) is None:
                expected[field] = rows[0].get(field)
    return canonical_offer_semantics(rows[0], rows[0].get("promo_offer_items") or []) == canonical_offer_semantics(expected, items)


def _coalesce_source_offer_items(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one shared-price source line together when extraction splits items."""
    grouped: list[dict[str, Any]] = []
    for offer in offers:
        terms = {key: value for key, value in offer.items() if key != "items"}
        match = next((row for row in grouped if (offer.get("is_package") is True or offer.get("purchase_type") == "bundle") and
            {key: value for key, value in row.items() if key != "items"} == terms), None)
        if match is None:
            grouped.append({**offer, "items": [dict(item) for item in offer.get("items") or []]})
            continue
        match["items"] = match["items"] + [
            dict(item) for item in offer.get("items") or [] if item not in match["items"]
        ]
    return grouped


def route_and_persist_extraction(
    client: Any,
    *,
    business_id: int,
    promotion_id: int,
    source_url: str,
    offers: list[dict[str, Any]],
    evidence: str,
    membership_plan_id: Optional[int] = None,
    seen_fingerprints: set[str] | None = None,
) -> dict[str, Any]:
    """Route list-price rows to clinic_services and discount rows to promo tables."""
    stats = {
        "services": 0,
        "promos": 0,
        "quarantined": 0,
        "skipped": 0,
        "service_writes": [],
        "promo_writes": [],
        "quarantine": [],
    }
    service_candidates: list[dict[str, Any]] = []
    normalized, purchase_reviews = normalize_purchase_offers(offers, evidence)
    stats["purchase_reviews"] = purchase_reviews
    rejected_reviews = [review for review in purchase_reviews if review["reason"] != "generic_offer_scope"]
    stats["quarantined"] += len(rejected_reviews)
    stats["quarantine"].extend(rejected_reviews)
    for offer in _coalesce_source_offer_items(normalized):
        kind = route_offer(offer)
        if kind == "service":
            item = offer_to_clinic_service_item(offer)
            if item:
                service_candidates.append(item)
            else:
                stats["skipped"] += 1
            continue
        if kind == "quarantine":
            stats["quarantined"] += 1
            stats["quarantine"].append(
                {
                    "reason": exclude_reason(offer) or "unroutable",
                    "offer_raw_text": str(offer.get("offer_raw_text") or "")[:200],
                }
            )
            continue
        write = persist_promo_offer(
            client,
            business_id=business_id,
            promotion_id=promotion_id,
            source_url=source_url,
            offer=offer,
            evidence=evidence,
            membership_plan_id=membership_plan_id,
            seen_fingerprints=seen_fingerprints,
        )
        stats["promo_writes"].append(write)
        if write.get("accepted"):
            stats["promos"] += 1
        elif write.get("action") == "quarantined":
            stats["quarantined"] += 1
            stats["quarantine"].append(
                {
                    "reason": write.get("reason") or "evidence_validation_failed",
                    "offer_raw_text": str(offer.get("offer_raw_text") or "")[:200],
                    "evidence_issues": write.get("evidence_issues") or [],
                }
            )
        else:
            stats["skipped"] += 1

    for item in pick_best_service_items(service_candidates, evidence, source_url=source_url):
        try:
            write = persist_service_item(
                client,
                business_id=business_id,
                item=item,
                source_url=source_url,
                evidence=evidence,
            )
        except Exception as exc:
            write = {"accepted": False, "reason": f"persist_error:{exc}", "action": "skipped"}
        stats["service_writes"].append(write)
        if write.get("accepted") and write.get("service_id"):
            stats["services"] += 1
    return stats
