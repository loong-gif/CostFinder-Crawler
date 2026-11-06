from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from utils.clinic_service_extraction import attach_service_ids_to_items
from utils.extraction_persist_pricing import _positive, build_master_from_offer, route_offer
from utils.extraction_persist_verification import _verify_offer_write
from utils.offer_evidence_validator import validate_offer_evidence
from utils.offer_fingerprint import normalize_offer_raw_text
from utils.offer_scope_filter import exclude_reason
from utils.promo_offer_items_db import is_blanket_offer_text, offer_item_semantics, upsert_offer_items
from utils.membership_duplicate_check import find_matching_membership_offer
from utils.schema_contract import TABLE_CLINIC_MEMBERSHIPS, TABLE_CLINIC_PROMOTIONS, TABLE_PROMO_OFFER_MASTER
from utils.offer_purchase_semantics import (
    canonical_offer_semantics, new_customer_tier_merge_patch, normalize_purchase_offers,
    offer_service_tier_identity, offer_update_identity, service_supported_by_source,
)


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
