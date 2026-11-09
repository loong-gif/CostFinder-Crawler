"""证据足以判定的情形由计划自动处理，不进入人工审查（用户裁定 2026-10-15、2026-11-07）。"""
import pytest

from monitor_price_plan_support import RAW, SOURCE, _evidence, _graph, _offer, _operation
from utils.monitor_price_plan import build_plan
from utils.offer_stage_spec import BENIGN_REASONS

COMPLETE = {"all_offers_extracted": True, "image_review_complete": True}


def _reasons(plan):
    return [row["reason"] for row in plan["review"]]


def test_currently_visible_offer_reactivates_its_inactive_record():
    graph = _graph()
    graph["offers"][0]["is_active"] = False
    plan = build_plan(SOURCE, graph, _evidence(), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    assert _operation(plan, "promo_offer_master", 4)["values"]["is_active"] is True


def test_inactive_promotion_on_the_same_source_is_reactivated_not_duplicated():
    graph = _graph()
    graph["promotions"][0].update(promotion_title="September specials", campaign_end_date="2026-09-30", is_active=False)
    graph["offers"][0]["is_active"] = False
    evidence = _evidence(promotion_title="October specials", promotion_end_date="2026-10-31")
    plan = build_plan(SOURCE, graph, evidence, {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    assert not any(op["action"] == "insert" for op in plan["operations"])
    values = _operation(plan, "clinic_promotions", 3)["values"]
    assert values["is_active"] is True and values["campaign_end_date"] == "2026-10-31"


def test_identical_duplicate_offers_keep_the_lowest_id_and_deactivate_the_rest():
    graph = _graph()
    graph["offers"] += [{**graph["offers"][0], "id": 6}]
    graph["items"] += [{**graph["items"][0], "offer_item_id": 8, "offer_id": 6}]
    plan = build_plan(SOURCE, graph, _evidence(offers=[_offer(discount_price=300, offer_raw_text=RAW.replace("$350", "$300"))]), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    assert _operation(plan, "promo_offer_master", 4)["values"]["discount_price"] == 300
    assert _operation(plan, "promo_offer_master", 6)["values"] == {"is_active": False}


def test_duplicate_quote_on_the_page_is_written_once():
    graph = _graph()
    graph.update(promotions=[], offers=[], items=[])
    plan = build_plan(SOURCE, graph, _evidence([_offer(), _offer()]), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    assert sum(op["table"] == "promo_offer_master" and op["action"] == "insert" for op in plan["operations"]) == 1


def test_offer_that_differs_only_by_membership_is_added_as_its_own_offer():
    member = _offer(offer_raw_text="Members: " + RAW, is_membership_required=True)
    plan = build_plan(SOURCE, _graph(), _evidence([member]), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    inserted = [op for op in plan["operations"] if op["table"] == "promo_offer_master" and op["action"] == "insert"]
    assert len(inserted) == 1 and inserted[0]["values"]["is_membership_required"] is True


def test_contradictory_savings_keep_the_stated_prices_and_drop_the_derived_amount():
    raw = "SAVE $250 Botox Now $1,400 (Reg. $1,750)"
    offer = _offer(offer_raw_text=raw, discount_price=1400, regular_price=1750, discount_amount=250,
                   items=[{"item_name": "Botox", "service_id": 12, "delivered_units": None}])
    plan = build_plan(SOURCE, _graph(), _evidence([offer]), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    values = _operation(plan, "promo_offer_master", 4)["values"]
    assert values["discount_price"] == 1400 and values["regular_price"] == 1750
    assert values.get("discount_amount") is None


def test_sale_price_above_regular_keeps_the_sale_price_without_a_regular_price():
    raw = "Botox 50 units Regular $300 Special $350"
    offer = _offer(offer_raw_text=raw, regular_price=300, discount_price=350)
    plan = build_plan(SOURCE, _graph(), _evidence([offer]), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    values = _operation(plan, "promo_offer_master", 4)["values"]
    assert values["regular_price"] is None and "discount_price" not in values


def test_offer_without_a_storable_price_is_filtered_not_reviewed():
    gift = _offer(offer_raw_text="Free consultation with any Botox treatment", regular_price=None,
                  discount_price=None, items=[{"item_name": "Botox", "service_id": 12}])
    plan = build_plan(SOURCE, _graph(), _evidence([_offer(), gift]), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    assert [note["reason"] for note in plan["notes"]] == ["no_supported_promo_fact"]
    assert "no_supported_discount_field" in BENIGN_REASONS


def test_member_price_without_a_non_member_price_stores_null_regular_price():
    raw = "Member special: Botox 50 units $350. Non-member price varies."
    offer = _offer(offer_raw_text=raw, regular_price=None, is_membership_required=True)
    plan = build_plan(SOURCE, _graph(), _evidence([offer]), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    inserted = next(op for op in plan["operations"] if op["table"] == "promo_offer_master")
    assert inserted["values"]["regular_price"] is None and inserted["values"]["discount_price"] == 350


def test_several_items_without_a_package_flag_are_written_as_a_package():
    raw = "Botox 50 units + Dysport 100 units Special $700"
    graph = _graph()
    graph["services"].append({"service_id": 13, "business_id": 7, "service_name": "Dysport", "service_area": None})
    offer = _offer(offer_raw_text=raw, regular_price=None, discount_price=700, is_package=False,
                   items=[{"item_name": "Botox", "service_id": 12, "delivered_units": 50},
                          {"item_name": "Dysport", "service_id": 13, "delivered_units": 100}])
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    inserted = next(op for op in plan["operations"] if op["table"] == "promo_offer_master" and op["action"] == "insert")
    assert inserted["values"]["is_package"] is True


@pytest.mark.parametrize("start,end", [("next week", None), ("2026-11-30", "2026-11-01")])
def test_unreadable_or_reversed_campaign_dates_are_stored_as_null(start, end):
    plan = build_plan(SOURCE, _graph(), _evidence(campaign_start_date=start, campaign_end_date=end), {})
    assert plan["status"] == "ready" and plan["review"] == [], plan


def test_month_and_day_without_a_year_mean_the_observed_year():
    evidence = _evidence(promotion_title="Specials", campaign_end_date="December 31", observed_date="2026-11-09")
    evidence["date_text"] = "Offers valid through December 31."
    plan = build_plan(SOURCE, _graph(), evidence, {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "clinic_promotions", 3)["values"]["campaign_end_date"] == "2026-12-31"


def test_yearless_campaign_already_over_retires_the_live_promotion():
    # 十一月看到的 "July special" 已经过期：页面上没有当前促销，现有记录下架
    evidence = _evidence(promotion_title="Specials", campaign_end_date="July 31", observed_date="2026-11-09", **COMPLETE)
    evidence["date_text"] = "July special: valid through July 31."
    plan = build_plan(SOURCE, _graph(), evidence, {})
    assert plan["status"] == "ready" and plan["review"] == [], plan
    assert _operation(plan, "promo_offer_master", 4)["values"] == {"is_active": False}
    assert _operation(plan, "clinic_promotions", 3)["values"] == {"is_active": False}


def test_expired_campaign_matching_several_promotions_retires_all_of_them():
    graph = _graph()
    graph["promotions"][0]["campaign_end_date"] = "2026-09-30"
    graph["promotions"].append({**graph["promotions"][0], "promotion_id": 9, "promotion_title": "Specials"})
    evidence = _evidence(promotion_title="Specials", promotion_end_date="2026-09-30", observed_date="2026-10-09", **COMPLETE)
    plan = build_plan(SOURCE, graph, evidence, {})
    assert "expired_campaign_identity_requires_review" not in _reasons(plan)
    retired = {op["id"] for op in plan["operations"] if op["table"] == "clinic_promotions"
               and op["values"].get("is_active") is False}
    assert retired == {3, 9}
