import pytest

from monitor_price_plan_support import RAW, SOURCE, _evidence, _graph, _offer, _operation, _with_extra_offer
from utils.monitor_price_plan import build_plan


def test_uncertain_price_skips_offer_without_clearing_original_or_borrowing_catalogue_price():
    offer = _offer(offer_raw_text="Special 20% off Botox", regular_price=450, discount_price=350,
                   discount_percent=20, items=[{"item_name": "Botox", "service_id": 12}])
    plan = build_plan(SOURCE, _graph(), _evidence([offer]), {})
    assert plan["status"] == "review" and not plan["operations"]
    assert plan["review"][0]["reason"] == "unsupported_price_evidence"
    assert plan["before"]["offers"][0]["discount_price"] == 350


def test_unsupported_quote_does_not_block_independent_valid_offer_or_allow_removal():
    offer = _offer(offer_raw_text=RAW.replace("$350", "$400"), discount_price=400)
    invalid = _offer(offer_raw_text="Special Botox $999", discount_price=999)
    evidence = _evidence([offer, invalid], markdown=offer["offer_raw_text"])
    plan = build_plan(SOURCE, _with_extra_offer(_graph()), evidence, {})
    assert plan["status"] == "ready"
    assert plan["review"][0]["reason"] == "offer_raw_text_not_supported"
    assert not any(op["values"].get("is_active") is False for op in plan["operations"])


def test_inactive_historical_promotion_is_never_resurrected_by_url():
    graph = _graph()
    graph["promotions"][0]["is_active"] = False
    graph["offers"][0]["is_active"] = False
    plan = build_plan(SOURCE, graph, _evidence(), {})
    assert plan["status"] == "ready", plan
    assert all(op["action"] == "insert" for op in plan["operations"])
    assert plan["before"]["promotions"][0]["is_active"] is False
    assert plan["operations"][0]["values"]["campaign_end_date"] is None


def test_distinct_current_campaign_inserts_promotion_without_rewriting_history():
    graph = _graph()
    graph["promotions"][0].update(promotion_title="September specials", campaign_end_date="2026-09-30", is_active=False)
    graph["offers"][0]["is_active"] = False
    evidence = _evidence(promotion_title="October specials", promotion_end_date="2026-10-31")
    plan = build_plan(SOURCE, graph, evidence, {})
    assert plan["status"] == "ready", plan
    assert all(op["action"] == "insert" for op in plan["operations"])
    assert plan["operations"][0]["values"]["campaign_end_date"] == "2026-10-31"
    assert plan["before"]["promotions"][0]["promotion_title"] == "September specials"


def test_two_campaigns_with_same_url_without_current_identity_require_review():
    graph = _graph()
    graph["promotions"] += [{**graph["promotions"][0], "promotion_id": 9}]
    plan = build_plan(SOURCE, graph, _evidence(), {})
    assert plan["status"] == "review" and not plan["operations"]


@pytest.mark.parametrize("mutate", [
    lambda g: g["offers"][0].update(business_id=8),
    lambda g: g["services"][0].update(business_id=8),
    lambda g: g["items"][0].update(offer_id=999),
    lambda g: g["offers"][0].update(membership_plan_id=10),
])
def test_cross_source_graph_relationships_are_rejected(mutate):
    graph = _graph()
    mutate(graph)
    plan = build_plan(SOURCE, graph, _evidence(), {})
    assert plan["status"] == "review" and not plan["operations"]


def test_evidence_from_different_source_is_rejected():
    plan = build_plan(SOURCE, _graph(), _evidence(url="https://other.example/specials"), {})
    assert plan["status"] == "review" and not plan["operations"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_nonfinite_negative_and_boolean_prices_are_rejected(value):
    plan = build_plan(SOURCE, _graph(), _evidence([_offer(discount_price=value)]), {})
    assert plan["status"] == "review" and not plan["operations"]


def test_explicit_visible_no_current_offers_can_deactivate_all():
    quote = "There are no current specials."
    evidence = _evidence([], markdown=quote, explicitly_empty=True, no_current_offers_quote=quote)
    plan = build_plan(SOURCE, _graph(), evidence, {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "promo_offer_master", 4)["values"] == {"is_active": False}
    assert _operation(plan, "clinic_promotions", 3)["values"]["is_active"] is False


@pytest.mark.parametrize("changes", [
    {"complete": False}, {"no_current_offers_quote": "No current specials"},
    {"no_current_offers_quote": "Welcome to our clinic."},
])
def test_empty_signal_requires_complete_supported_visible_quote(changes):
    quote = "There are no current specials."
    evidence = _evidence([], markdown=quote, explicitly_empty=True, no_current_offers_quote=quote)
    evidence.update(changes)
    # The unsupported quote is deliberately distinct from the page.
    if changes.get("no_current_offers_quote") == "No current specials":
        evidence["no_current_offers_quote"] = "No active promotions"
    plan = build_plan(SOURCE, _graph(), evidence, {})
    assert plan["status"] == "review" and not plan["operations"]


def test_explicit_empty_cascade_respects_protected_item():
    quote = "There are no current specials."
    evidence = _evidence([], markdown=quote, explicitly_empty=True, no_current_offers_quote=quote)
    plan = build_plan(SOURCE, _graph(), evidence, {"item_ids": [5]})
    assert plan["status"] == "review" and not plan["operations"]


def test_expired_campaign_evidence_cannot_create_or_reactivate_offers():
    evidence = _evidence(promotion_title="September specials", promotion_end_date="2026-09-30",
                         observed_date="2026-10-09")
    plan = build_plan(SOURCE, _graph(), evidence, {})
    assert plan["status"] == "review" and not plan["operations"]
    assert plan["review"][-1]["reason"] == "expired_campaign_evidence"


def test_unknown_current_dates_clear_old_dates_without_blocking():
    graph = _graph()
    graph["promotions"][0]["campaign_end_date"] = "2026-10-31"
    plan = build_plan(SOURCE, graph, _evidence(promotion_title="Specials", observed_date="2026-10-09"), {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "clinic_promotions", 3)["values"]["campaign_end_date"] is None


@pytest.mark.parametrize("yearless", ["10/31", "October 31"])
def test_yearless_model_date_is_null_instead_of_a_write_blocker(yearless):
    graph = _graph()
    graph["promotions"][0]["campaign_end_date"] = "2026-09-30"
    plan = build_plan(SOURCE, graph, _evidence(campaign_end_date=yearless), {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "clinic_promotions", 3)["values"]["campaign_end_date"] is None


def test_unmapped_existing_offer_price_change_preserves_offer_and_item_ids():
    graph = _graph()
    graph["items"][0]["service_id"] = None
    graph["services"] = []
    offer = _offer(offer_raw_text="Botox 50 units Regular $450 Special $400", discount_price=400)
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    assert not any(op["action"] == "insert" for op in plan["operations"])
    assert _operation(plan, "promo_offer_master", 4)["values"]["discount_price"] == 400
    assert _operation(plan, "promo_offer_items", 5)["values"]["unit_price"] == 8
    assert _operation(plan, "clinic_promotions", 3)["values"]["is_offer_extracted"] is False


def test_new_evidenced_offer_needs_neither_year_nor_catalogue_mapping():
    graph = _graph()
    graph.update(promotions=[], offers=[], items=[], services=[])
    plan = build_plan(SOURCE, graph, _evidence(), {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "clinic_promotions", -1)["values"]["campaign_end_date"] is None
    assert _operation(plan, "promo_offer_items", -1)["values"]["service_id"] is None


def test_percent_only_discount_is_not_a_starting_price():
    offer = _offer(regular_price=None, discount_price=None, discount_percent=20,
                   price_model="from", offer_raw_text="Special 20% off Botox",
                   items=[{"item_name": "Botox", "delivered_units": None}])
    graph = _graph()
    graph.update(promotions=[], offers=[], items=[], services=[])
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "promo_offer_master", -1)["values"]["price_model"] == "total"


def test_unknown_year_does_not_inherit_expired_parent_date():
    graph = _graph()
    graph["promotions"][0]["campaign_end_date"] = "2026-09-30"
    plan = build_plan(SOURCE, graph, _evidence(promotion_title="Specials", observed_date="2026-10-11"), {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "clinic_promotions", 3)["values"]["campaign_end_date"] is None


def test_model_cannot_borrow_footer_year_for_a_yearless_campaign_date():
    graph = _graph()
    graph["promotions"][0]["campaign_end_date"] = "2026-09-30"
    evidence = _evidence(promotion_title="Specials", campaign_end_date="2026-10-31")
    evidence["date_text"] = "Offers valid through 10/31. Copyright 2026."
    plan = build_plan(SOURCE, graph, evidence, {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "clinic_promotions", 3)["values"]["campaign_end_date"] is None
    evidence["date_text"] = "Offers valid through October 31, 2026."
    plan = build_plan(SOURCE, graph, evidence, {})
    assert _operation(plan, "clinic_promotions", -1)["values"]["campaign_end_date"] == "2026-10-31"


def test_explicit_two_digit_expired_year_is_not_cleared_as_unknown():
    evidence = _evidence(campaign_end_date="2026-09-30", observed_date="2026-10-11")
    evidence["date_text"] = "Expires 09/30/26"
    plan = build_plan(SOURCE, _graph(), evidence, {})
    assert plan["status"] == "review"
    assert plan["review"][-1]["reason"] == "expired_campaign_evidence"
