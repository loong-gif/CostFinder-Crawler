from __future__ import annotations

from utils.monitor_price_plan_campaign import _campaign
from utils.monitor_price_plan_common import _Review, _text
from utils.monitor_price_plan_offer import _resolve_items, _term_values, _validated_offer
from utils.monitor_price_plan_variant import _variant_catalog_complete, _variant_reconciliation_records
from utils.monitor_price_writer_plan import WriterError
from utils.offer_stage_spec import BENIGN_REASONS
from utils.promo_offer_items_db import is_blanket_offer_text


def collect_expected_offers(raw_offers: list, evidence: dict, markdown: str, observed_date: str | None,
                            promotions: list[dict], services: list[dict], business_id: int,
                            plan: dict, warning, expired_campaigns: list) -> tuple[list, int, set[str]]:
    """返回 (当前 offer, 不构成促销的条数, 已过期 offer 的原文)。"""
    expected: list[tuple[dict, list[dict], dict, dict, int]] = []
    non_promo = 0
    expired_quotes: set[str] = set()
    for index, raw in enumerate(raw_offers):
        if not isinstance(raw, dict):
            warning("invalid_offer_evidence", index)
            continue
        verified_image = raw.get("visibility") == "image_only" and (
            evidence.get("image_review_complete") is True or
            (evidence.get("image_policy", {}).get("version") == 1 and raw.get("dual_read_verified") is True))
        if raw.get("visibility") != "visible_match" and not verified_image:
            reason = "extracted_quote_not_in_verified_text" if evidence.get("prior_visual_receipt") else "unresolved_image_or_visibility"
            warning(reason, index)
            continue
        try:
            resolved_items = _resolve_items(raw, services, business_id)
            master, reason = _validated_offer(raw, markdown, resolved_items)
            if master is None and reason in BENIGN_REASONS:
                # 赠品、没有可存价格的条目不是可写的 offer：过滤，不存也不审查
                non_promo += 1
                plan["notes"].append({"reason": reason, "offer_index": index, "blocking": False})
                continue
            if master is None:
                warning(reason, index)
                continue
            if not is_blanket_offer_text(master["offer_raw_text"]) and any(i["service_id"] is None for i in resolved_items):
                plan["notes"].append({"reason": "service_mapping_pending", "offer_index": index, "blocking": False})
            campaign = _campaign(raw, evidence)
            if observed_date and campaign["campaign_end_date"] and campaign["campaign_end_date"] < observed_date:
                # 已过期的活动不写入；同一结束日期（和标题）的现有促销全部下架
                matches = [p for p in promotions if p.get("is_active") is True
                           and p.get("campaign_end_date") == campaign["campaign_end_date"]
                           and (not campaign["promotion_title"] or _text(p.get("promotion_title")) == _text(campaign["promotion_title"]))]
                expired_campaigns.extend(p["promotion_id"] for p in matches)
                non_promo += 1
                expired_quotes.add(_text(raw.get("offer_raw_text")))
                plan["notes"].append({"reason": "expired_campaign_not_written", "offer_index": index,
                                      "promotion_ids": [p["promotion_id"] for p in matches], "blocking": False})
                continue
            if master["price_model"] == "from":
                if _variant_reconciliation_records(evidence) and not _variant_catalog_complete(evidence):
                    warning("starting_price_variant_set_incomplete", index)
                    continue
                plan["notes"].append({"reason": "starting_price_variant_set_incomplete",
                                      "offer_index": index, "blocking": False})
                plan["evidence_summary"]["complete"] = False
            terms = _term_values(master)
            expected.append((master, resolved_items, campaign, terms, index))
        except (_Review, WriterError) as exc:
            warning(str(exc), index)
    return expected, non_promo, expired_quotes
