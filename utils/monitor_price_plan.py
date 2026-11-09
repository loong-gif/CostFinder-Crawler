"""Pure, source-scoped planning for promotion price synchronization.

No writes or clocks: callers supply the source graph and current page evidence.
Negative IDs are references local to each table, resolved by the locked writer.
``review`` plans never contain operations. Ready plans can contain warnings;
warnings inhibit removal. The database trigger derives ``is_offer_extracted`` from related offers.
"""
from __future__ import annotations

from copy import deepcopy
import re
from urllib.parse import urlparse

from utils.offer_fingerprint import normalize_url
from utils.monitor_price_writer_plan import WriterError, normalize_source_url
from utils.monitor_rendered_browser import BREAKPOINT_NAMES
from utils.promo_offer_items_db import is_blanket_offer_text
from utils.offer_purchase_semantics import new_customer_tier_merge_patch
from utils.monitor_price_plan_campaign import _campaign, _campaign_key, _date
from utils.monitor_price_plan_common import (
    ITEMS, OFFERS, PROMOTIONS, TERMS, _Review, _id, _raw_identity, _service_name_key, _text,
)
from utils.monitor_price_plan_expected import collect_expected_offers
from utils.monitor_price_plan_identity import (
    _catalog_items, _identity, _nullable_item_match, _offer_match_key, _promotion_content,
    check_protected_rows, identical_offers, match_existing_item, select_existing_promotion,
)
from utils.offer_stage_spec import BENIGN_REASONS
from utils.monitor_price_plan_offer import _term_values
from utils.monitor_price_plan_variant import _check_variant_reconciliation


def build_plan(source: dict, graph: dict, evidence: dict, protections: dict) -> dict:
    """Build a reviewable plan without mutating any argument or database.

    Source-wide ambiguity, invalid relationships,
    and touched protected rows block every operation. Bad/image-only offer
    evidence instead permits validated additive work and inhibits all removal.
    """
    plan = {"schema_version": 1, "status": "ready", "source": deepcopy(source),
            "before": deepcopy(graph), "operations": [], "review": [], "notes": [],
            "evidence_summary": {"complete": evidence.get("complete") is True,
                                 "accessible": evidence.get("accessible") is True}}
    if evidence.get("image_policy") is not None:
        plan["image_policy"] = deepcopy(evidence["image_policy"])
        plan["image_retry_reason"] = evidence.get("image_retry_reason")
    operations = plan["operations"]
    next_ids = {PROMOTIONS: -1, OFFERS: -1, ITEMS: -1}

    def emit(table: str, values: dict, existing: dict | None = None) -> int:
        pk = ("promotion_id" if table == PROMOTIONS else "offer_item_id" if table == ITEMS
              else "offer_id" if table == TERMS else "id")
        if existing is None:
            row_id = values["offer_id"] if table == TERMS else next_ids[table]
            if table != TERMS:
                next_ids[table] -= 1
            operations.append({"table": table, "action": "insert", "id": row_id, "values": values})
        else:
            row_id = existing[pk]
            changes = {k: v for k, v in values.items() if existing.get(k) != v}
            if changes:
                operations.append({"table": table, "action": "update", "id": row_id, "values": changes})
        return row_id

    def warning(reason: str, index: int | None = None) -> None:
        plan["review"].append({"reason": reason, "offer_index": index, "blocking": False})
        plan["evidence_summary"]["complete"] = False

    try:
        business_id = _id(source.get("business_id"))
        url = normalize_url(source.get("url"))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _Review("invalid_source_url")
        source_key = normalize_source_url(source.get("url"))
        if normalize_source_url(evidence.get("url")) != source_key:
            raise _Review("evidence_source_mismatch")
        for key in ("promotions", "offers", "items", "services"):
            if not isinstance(graph.get(key), list) or any(not isinstance(r, dict) for r in graph[key]):
                raise _Review(f"invalid_graph:{key}")
        promotions, offers, items, services = (graph[k] for k in ("promotions", "offers", "items", "services"))
        for rows, pk in ((promotions, "promotion_id"), (offers, "id"), (items, "offer_item_id"), (services, "service_id")):
            ids = [_id(r.get(pk)) for r in rows]
            if len(ids) != len(set(ids)):
                raise _Review("duplicate_graph_id")
        promotion_ids = {p["promotion_id"] for p in promotions}
        offer_ids = {o["id"] for o in offers}
        if any(p.get("business_id") != business_id or normalize_source_url(p.get("source_url")) != source_key for p in promotions):
            raise _Review("source_business_mismatch")
        if any(o.get("business_id") != business_id or o.get("promotion_id") not in promotion_ids or o.get("membership_plan_id") is not None for o in offers):
            raise _Review("invalid_offer_relationship")
        if any(i.get("offer_id") not in offer_ids for i in items) or any(s.get("business_id") != business_id for s in services):
            raise _Review("invalid_item_or_service_relationship")
        source_promotion_id = source.get("promotion_id")
        if source_promotion_id is not None and _id(source_promotion_id) not in promotion_ids:
            raise _Review("source_promotion_not_found")
        markdown = str(evidence.get("markdown") or "")
        observed_date = _date(evidence.get("observed_date"))
        coverage = evidence.get("breakpoint_coverage")
        if coverage is not None and (set(coverage) != BREAKPOINT_NAMES
                or not all(row.get("complete") is True for row in coverage.values())):
            warning("breakpoint_coverage_incomplete")
        for review in evidence.get("semantic_reviews") or []:
            if review.get("reason") not in BENIGN_REASONS:
                warning(review.get("reason", "purchase_semantics_requires_review"))
        raw_offers = evidence.get("offers")
        if not isinstance(raw_offers, list):
            raise _Review("invalid_evidence_offers")
        _check_variant_reconciliation(evidence, raw_offers, warning)
        empty_quote = str(evidence.get("no_current_offers_quote") or "").strip()
        explicitly_empty = (not raw_offers and evidence.get("explicitly_empty") is True
                            and plan["evidence_summary"]["complete"] and bool(empty_quote)
                            and _text(empty_quote) in _text(markdown)
                            and bool(re.search(r"\bno\b.{0,50}\b(?:specials?|promotions?|offers?)\b|\b(?:specials?|promotions?|offers?)\b.{0,50}\bnot\s+(?:currently\s+)?available\b", empty_quote, re.I)))
        current_quotes = [_text(o.get("offer_raw_text")) for o in offers if o.get("is_active") is True]
        page_campaign = _campaign({}, evidence)
        page_expired_matches = []
        if not raw_offers and observed_date and page_campaign["campaign_end_date"] and page_campaign["campaign_end_date"] < observed_date:
            # 页面只剩已过期的活动：同一结束日期（和标题）的现有促销全部下架
            page_expired_matches = [p["promotion_id"] for p in promotions if p.get("is_active") is True
                                    and p.get("campaign_end_date") == page_campaign["campaign_end_date"]
                                    and (not page_campaign["promotion_title"] or _text(p.get("promotion_title")) == _text(page_campaign["promotion_title"]))]

        def without_offers_verified(expired_quotes=frozenset()) -> bool:
            # 页面上仍出现的现有 offer 原文属于已过期活动时，不妨碍判定页面没有当前促销
            return (evidence.get("complete") is True and evidence.get("all_offers_extracted") is True
                    and evidence.get("accessible") is True and evidence.get("image_review_complete") is True
                    and (bool(page_expired_matches) or all(q and (q in expired_quotes or q not in _text(markdown))
                                                           for q in current_quotes)))
        page_without_offers_verified = without_offers_verified()
        page_empty_verified = not raw_offers and page_without_offers_verified
        nothing_live = (not any(p.get("is_active") is True for p in promotions)
                        and not any(o.get("is_active") is True for o in offers))
        # An accessible page without offers cannot change a source that has nothing live: no-op, not review.
        if not raw_offers and plan["evidence_summary"]["accessible"] and markdown.strip() and nothing_live:
            plan["notes"].append({"reason": "no_offers_and_no_live_rows", "blocking": False})
            return plan
        if not plan["evidence_summary"]["accessible"] or (not markdown.strip() and not page_empty_verified) or (not raw_offers and not explicitly_empty and not page_empty_verified):
            raise _Review("inaccessible_empty_or_unresolved_evidence")
        if evidence.get("image_policy", {}).get("hold_parent_fields"):
            warning("image_candidates_shadow_or_unresolved")
        existing_items = {o["id"]: [i for i in items if i["offer_id"] == o["id"]] for o in offers}
        expired_campaigns = list(page_expired_matches)
        expected, non_promo, expired_quotes = collect_expected_offers(
            raw_offers, evidence, markdown, observed_date, promotions, services, business_id,
            plan, warning, expired_campaigns)
        # A page whose only extracted lines are regular prices or expired campaigns carries no promotion: an empty page.
        if raw_offers and not expected and non_promo == len(raw_offers):
            if nothing_live:
                plan["notes"].append({"reason": "no_offers_and_no_live_rows", "blocking": False})
                return plan
            page_empty_verified = without_offers_verified(expired_quotes)
        expired_page_verified = bool(expired_campaigns and plan["evidence_summary"]["complete"] and not plan["review"])
        if not expected and not explicitly_empty and not page_empty_verified and not expired_page_verified:
            raise _Review(plan["review"][-1]["reason"] if plan["review"] else "no_validated_offers")
        complete = (plan["evidence_summary"]["complete"] and not plan["review"]
                    and (explicitly_empty or page_empty_verified or expired_page_verified or bool(expected)))
        if not complete:
            plan["evidence_summary"]["complete"] = False
        selected_promotions: dict[tuple, int] = {}
        promotion_quotes: dict[tuple, list[str]] = {}
        successful_campaigns: set[tuple] = set()
        for master, _, campaign, _, _ in expected:
            promotion_quotes.setdefault(_campaign_key(campaign), []).append(master["offer_raw_text"])
        matched: set[int] = set()
        expected_identities: set[tuple] = set()
        for master, desired_items, campaign, terms, offer_index in expected:
            campaign_key = _campaign_key(campaign)
            expected_identity = (campaign_key, _identity(master, desired_items),
                                 _raw_identity(master["offer_raw_text"]))
            if expected_identity in expected_identities:
                # 页面上同一条 offer 出现两次：只写一次
                continue
            expected_identities.add(expected_identity)
            if campaign_key not in selected_promotions:
                existing_promotion = select_existing_promotion(promotions, campaign, source_promotion_id)
                if existing_promotion is not None and existing_promotion["promotion_id"] in selected_promotions.values():
                    # 一个来源只有一行促销：同页的另一组活动字段并入同一行
                    selected_promotions[campaign_key] = existing_promotion["promotion_id"]
            if campaign_key not in selected_promotions:
                promo_values = {"business_id": business_id,
                                "source_url": existing_promotion["source_url"] if existing_promotion else url,
                                "is_active": True,
                                "promotion_content": _promotion_content(existing_promotion, promotion_quotes[campaign_key], complete),
                                **{k: v for k, v in campaign.items() if v is not None or k != "promotion_title"}}
                if evidence.get("image_policy", {}).get("hold_parent_fields") and existing_promotion is not None:
                    promo_values = {"is_active": True}
                if existing_promotion is None:
                    promo_values.setdefault("promotion_title", url)
                    promo_values.setdefault("campaign_start_date", None)
                    promo_values.setdefault("campaign_end_date", None)
                selected_promotions[campaign_key] = emit(PROMOTIONS, promo_values, existing_promotion)
            promotion_id = selected_promotions[campaign_key]
            candidates = [o for o in offers if o["promotion_id"] == promotion_id
                          and _offer_match_key(o, existing_items[o["id"]]) == _offer_match_key(master, desired_items)]
            if len(candidates) > 1:
                # Several bundles of one service can coexist; a unique fixed
                # count/threshold preserves its ID without using the changed price.
                quantities = sorted((str(i.get("service_id") or _service_name_key(i.get("item_name"))),
                                     i.get("delivered_units")) for i in desired_items)
                narrower = [o for o in candidates if sorted(
                    (str(i.get("service_id") or _service_name_key(i.get("item_name"))), i.get("delivered_units"))
                    for i in existing_items[o["id"]]) == quantities
                    and _term_values(o)["min_quantity"] == terms["min_quantity"]]
                if len(narrower) == 1:
                    candidates = narrower
            if not candidates and not is_blanket_offer_text(master["offer_raw_text"]):
                candidates = [o for o in offers if o["promotion_id"] == promotion_id
                              and _nullable_item_match(o, _catalog_items(existing_items[o["id"]], services), master, desired_items)]
            if len(candidates) > 1 and raw_offers[offer_index].get("purchase_variant"):
                exact = [o for o in candidates if _raw_identity(o.get("offer_raw_text")) == _raw_identity(master["offer_raw_text"])]
                if len(exact) == 1:
                    candidates = exact
            if len(candidates) > 1:
                duplicates = identical_offers(candidates, existing_items)
                if not duplicates:
                    warning("ambiguous_offer_match", offer_index)
                    continue
                # 内容完全相同的重复 Offer：保留最小 ID，其余停用
                candidates = duplicates[:1]
                for extra in duplicates[1:]:
                    matched.add(extra["id"])
                    if extra.get("is_active") is True:
                        emit(OFFERS, {"is_active": False}, extra)
            # 唯一对应的失效记录随当前页面重新激活（master_values 带 is_active=True）
            existing_offer = candidates[0] if candidates else None
            if existing_offer and existing_offer["id"] in matched:
                warning("ambiguous_offer_match", offer_index)
                continue
            successful_campaigns.add(campaign_key)
            master_values = {**master, "business_id": business_id, "promotion_id": promotion_id, "membership_plan_id": None}
            if existing_offer is not None:
                tier = new_customer_tier_merge_patch(existing_offer, master)
                if tier:
                    master_values = {**existing_offer, **master_values, **tier}
            offer_id = emit(OFFERS, master_values, existing_offer)
            if existing_offer:
                matched.add(offer_id)
            old_items = existing_items.get(offer_id, [])
            blanket = is_blanket_offer_text(master["offer_raw_text"])
            for desired in desired_items:
                desired, existing_item = match_existing_item(old_items, desired, blanket, services)
                emit(ITEMS, {**desired, "offer_id": offer_id}, existing_item)
            current_term = next((t for t in graph.get("terms", []) if t.get("offer_id") == offer_id), None)
            term_payload = {**terms, "offer_id": offer_id}
            has_terms = bool(terms["raw_terms"] or terms["min_quantity"] is not None
                             or terms["is_one_per_patient"] or terms["is_new_customer_required"]
                             or terms["is_membership_required"])
            if current_term is not None or has_terms:
                emit(TERMS, term_payload, current_term)
        successful_promotion_ids = {selected_promotions[key] for key in successful_campaigns}
        operations[:] = [op for op in operations if op["table"] != PROMOTIONS
                         or op["id"] in successful_promotion_ids]
        if plan["review"]:
            complete = False
            plan["evidence_summary"]["complete"] = False
        if complete:
            for offer in offers:
                if offer.get("is_active") is True and offer["id"] not in matched and (expected or not expired_page_verified or offer["promotion_id"] in expired_campaigns):
                    emit(OFFERS, {"is_active": False}, offer)
            for promotion in promotions:
                if promotion.get("is_active") is True and promotion["promotion_id"] not in selected_promotions.values() and (expected or not expired_page_verified or promotion["promotion_id"] in expired_campaigns):
                    emit(PROMOTIONS, {"is_active": False}, promotion)
        elif page_empty_verified or explicitly_empty:
            for offer in offers:
                if offer.get("is_active") is True:
                    emit(OFFERS, {"is_active": False}, offer)
            for promotion in promotions:
                if promotion.get("is_active") is True:
                    emit(PROMOTIONS, {"is_active": False}, promotion)
        check_protected_rows(operations, protections, offers, items, existing_items)
        if plan["review"] and not operations:
            plan["status"] = "review"
    except (_Review, WriterError) as exc:
        plan["status"] = "review"
        plan["operations"] = []
        plan["review"].append({"reason": str(exc), "blocking": True})
    return plan
