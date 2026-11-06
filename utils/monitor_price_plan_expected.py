from __future__ import annotations

import re

from utils.monitor_price_plan_campaign import _campaign
from utils.monitor_price_plan_common import _Review, _text
from utils.monitor_price_plan_offer import _resolve_items, _term_values, _validated_offer
from utils.monitor_price_plan_variant import _variant_catalog_complete, _variant_reconciliation_records
from utils.monitor_price_writer_plan import WriterError
from utils.promo_offer_items_db import is_blanket_offer_text


_NON_MEMBER = re.compile(r"\bnon[- ]?members?\b", re.I)


def collect_expected_offers(raw_offers: list, evidence: dict, markdown: str, observed_date: str | None,
                            promotions: list[dict], services: list[dict], business_id: int,
                            plan: dict, warning, expired_campaigns: list) -> tuple[list, int]:
    expected: list[tuple[dict, list[dict], dict, dict, int]] = []
    non_promo = 0
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
            if master is None and reason == "no_supported_promo_fact":
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
                matches = [p for p in promotions if p.get("is_active") is True
                           and p.get("campaign_end_date") == campaign["campaign_end_date"]
                           and (not campaign["promotion_title"] or _text(p.get("promotion_title")) == _text(campaign["promotion_title"]))]
                if len(matches) != 1:
                    warning("expired_campaign_evidence" if not matches else "expired_campaign_identity_requires_review", index)
                else:
                    expired_campaigns.append(matches[0]["promotion_id"])
                    plan["notes"].append({"reason":"expired_campaign_verified", "promotion_id":matches[0]["promotion_id"], "blocking":False})
                continue
            if master["price_model"] == "from":
                if _variant_reconciliation_records(evidence) and not _variant_catalog_complete(evidence):
                    warning("starting_price_variant_set_incomplete", index)
                    continue
                plan["notes"].append({"reason": "starting_price_variant_set_incomplete",
                                      "offer_index": index, "blocking": False})
                plan["evidence_summary"]["complete"] = False
            if raw.get("is_membership_required") and raw.get("regular_price") is None:
                promo_blob = _text(markdown)
                if _NON_MEMBER.search(promo_blob) or _NON_MEMBER.search(str(raw.get("offer_raw_text") or "")):
                    warning("member_regular_price_missing", index)
            terms = _term_values(master)
            expected.append((master, resolved_items, campaign, terms, index))
        except (_Review, WriterError) as exc:
            warning(str(exc), index)
    return expected, non_promo
