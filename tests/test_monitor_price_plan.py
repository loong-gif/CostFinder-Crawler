"""Focused offline contracts for source-scoped price synchronization plans."""
from copy import deepcopy

import pytest

from utils.monitor_price_plan import build_plan


SOURCE = {"business_id": 7, "url": "https://clinic.example/specials"}
RAW = "Botox 50 units Regular $450 Special $350"


def _offer(**changes):
    value = {"offer_raw_text": RAW, "regular_price": 450, "discount_price": 350,
             "discount_percent": None, "discount_amount": None, "price_model": "total",
             "is_package": False, "is_membership_required": False,
             "is_new_customer_required": False, "conditions": [],
             "visibility": "visible_match", "quantity_kind": "fixed",
             "items": [{"item_name": "Botox", "service_id": 12, "delivered_units": 50, "unit_price": 7}]}
    return {**value, **changes}


def _evidence(offers=None, **changes):
    offers = [_offer()] if offers is None else offers
    dates = [v for k, v in changes.items() if k in {"campaign_start_date", "campaign_end_date", "promotion_start_date", "promotion_end_date"} and v]
    return {"url": SOURCE["url"], "accessible": True, "complete": True,
            "markdown": "\n".join([*(o.get("offer_raw_text", "") for o in offers), *dates]), "offers": offers, **changes}


def _graph():
    master = {k: v for k, v in _offer().items() if k not in {"visibility", "quantity_kind", "items"}}
    return {"promotions": [{"promotion_id": 3, "business_id": 7, "source_url": SOURCE["url"],
                            "promotion_title": "Specials", "campaign_start_date": None,
                            "campaign_end_date": None, "promotion_content": [RAW],
                            "is_active": True, "is_offer_extracted": True}],
            "offers": [{**master, "id": 4, "business_id": 7, "promotion_id": 3,
                        "membership_plan_id": None, "is_active": True}],
            "items": [{"offer_item_id": 5, "offer_id": 4, "service_id": 12,
                       "item_name": "Botox", "delivered_units": 50, "unit_price": 7}],
            "services": [{"service_id": 12, "business_id": 7, "service_name": "Botox", "service_area": None}]}


def _operation(plan, table, row_id):
    return next(op for op in plan["operations"] if op["table"] == table and op["id"] == row_id)


def test_unchanged_is_noop_and_arguments_are_immutable():
    args = (SOURCE, _graph(), _evidence(), {})
    before = deepcopy(args)
    plan = build_plan(*args)
    assert plan["status"] == "ready"
    assert plan["operations"] == []
    assert args == before
    assert plan["before"] == before[1]
    plan["before"]["offers"][0]["discount_price"] = 999
    assert args[1]["offers"][0]["discount_price"] == 350


def test_price_change_preserves_master_and_item_ids():
    offer = _offer(offer_raw_text=RAW.replace("$350", "$400"), discount_price=400)
    plan = build_plan(SOURCE, _graph(), _evidence([offer]), {})
    assert plan["status"] == "ready"
    assert all(op["action"] == "update" for op in plan["operations"])
    assert _operation(plan, "promo_offer_master", 4)["values"]["discount_price"] == 400
    assert _operation(plan, "promo_offer_items", 5)["values"]["unit_price"] == 8
    assert not any("offer_fingerprint" in op["values"] or "updated_at" in op["values"] for op in plan["operations"])


def test_inserted_item_units_are_integer_for_smallint_column():
    graph = _graph()
    graph["offers"], graph["items"] = [], []
    offer = _offer(items=[{"item_name": "Botox", "service_id": 12, "delivered_units": 50.0, "unit_price": None}])
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    inserted = [op["values"] for op in plan["operations"] if op["table"] == "promo_offer_items" and op["action"] == "insert"]
    assert inserted and all(type(v["delivered_units"]) is int for v in inserted)


def _changed_condition_offer():
    return _offer(offer_raw_text=RAW + ". Not valid with other offers", conditions=["Not valid with other offers"])


def test_condition_paraphrase_updates_existing_offer_id_and_terms():
    graph=_graph()
    graph["offers"][0]["conditions"]=["Cannot be combined"]
    plan=build_plan(SOURCE,graph,_evidence([_changed_condition_offer()]),{})
    assert plan["status"] == "ready"
    assert not any(op["table"] == "promo_offer_master" and op["action"] == "insert" for op in plan["operations"])
    assert any(op["table"] == "promo_offer_terms" and op["id"] == 4 for op in plan["operations"])


def test_minimum_purchase_and_one_per_patient_are_written_to_existing_terms():
    graph = _graph()
    graph["offers"][0]["is_new_customer_required"] = True
    graph["terms"] = [{"offer_id":4,"raw_terms":[],"min_quantity":None,"min_unit":None,
                       "is_one_per_patient":False,"is_new_customer_required":False,
                       "is_membership_required":False}]
    raw = "Botox Regular $450 Special $350. Minimum 40 units. One per patient. New patient only."
    offer = _offer(offer_raw_text=raw, is_new_customer_required=True, quantity_kind="minimum",
                   conditions=["Minimum 40 units", "One per patient"],
                   items=[{"item_name":"Botox","service_id":12,"delivered_units":None,"unit_price":None}])
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    op = next(op for op in plan["operations"] if op["table"] == "promo_offer_terms")
    assert op["action"] == "update" and op["id"] == 4
    assert op["values"]["min_quantity"] == 40
    assert op["values"]["min_unit"] == "unit"
    assert op["values"]["is_one_per_patient"] is True
    assert op["values"]["is_new_customer_required"] is True


@pytest.mark.parametrize("reverse",[False,True])
def test_duplicate_scope_candidate_is_reviewed_without_blocking_clear_offer(reverse):
    offers=[_offer(),_changed_condition_offer()]
    if reverse: offers.reverse()
    plan=build_plan(SOURCE,_graph(),_evidence(offers),{})
    assert plan["status"] == "ready",plan
    assert sum(op["table"]=="promo_offer_master" and op["action"]=="insert" for op in plan["operations"]) == 0
    assert any(row["reason"] == "ambiguous_offer_match" for row in plan["review"])
    assert not any(op["values"].get("is_active") is False for op in plan["operations"])
    assert _operation(plan,"clinic_promotions",3)["values"]["is_offer_extracted"] is False


def test_different_pricing_basis_preserves_existing_offer():
    graph=_graph()
    graph["offers"][0].update(conditions=["Cannot be combined"],price_model="per_unit")
    plan=build_plan(SOURCE,graph,_evidence([_changed_condition_offer()]),{})
    assert plan["status"] == "ready",plan
    assert not any(op["table"]=="promo_offer_master" and op["action"]=="insert" for op in plan["operations"])


def test_inactive_offer_is_not_restored_and_clear_sibling_can_update():
    graph=_with_extra_offer(_graph())
    graph["offers"][0]["is_active"] = False
    sibling=_offer(offer_raw_text="Dysport Special $240", regular_price=None, discount_price=240,
                   items=[{"service_id":13,"item_name":"Dysport","delivered_units":None,"unit_price":240}])
    plan=build_plan(SOURCE,graph,_evidence([_offer(),sibling]),{})
    assert plan["status"] == "ready",plan
    assert not any(op["table"] == "promo_offer_master" and op["id"] == 4 for op in plan["operations"])
    assert _operation(plan,"promo_offer_master",6)["values"]["discount_price"] == 240
    assert _operation(plan,"clinic_promotions",3)["values"]["is_offer_extracted"] is False
    assert any(row["reason"] == "historical_offer_requires_review" for row in plan["review"])


def test_fixed_unit_prices_and_regular_prices_are_converted_to_totals():
    raw = "Botox 50 units Special $7/unit regular $9/unit"
    offer = _offer(offer_raw_text=raw, regular_price=9, discount_price=7, price_model="per_unit")
    graph = _graph()
    graph["offers"][0].update(regular_price=9, discount_price=7, price_model="per_unit")
    graph["items"][0]["unit_price"] = 0.14
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "promo_offer_master", 4)["values"]["discount_price"] == 350
    assert _operation(plan, "promo_offer_master", 4)["values"]["regular_price"] == 450
    assert _operation(plan, "promo_offer_items", 5)["values"]["unit_price"] == 7


@pytest.mark.parametrize("kind,wording", [("minimum", "minimum 40 units"), ("variable", "up to 40 units")])
def test_bound_quantities_keep_source_unit_price_without_total_arithmetic(kind, wording):
    raw = f"Botox Special $11/unit regular $13/unit, {wording}"
    offer = _offer(offer_raw_text=raw, regular_price=13, discount_price=11,
                   price_model="per_unit", quantity_kind=kind,
                   items=[{"item_name": "Botox", "service_id": 12, "delivered_units": 40, "unit_price": 11}])
    plan = build_plan(SOURCE, _graph(), _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "promo_offer_master", 4)["values"]["discount_price"] == 11
    values = _operation(plan, "promo_offer_items", 5)["values"]
    assert values["delivered_units"] is None and values["unit_price"] == 11


def test_package_switch_preserves_item_id_and_explicitly_clears_price():
    plan = build_plan(SOURCE, _graph(), _evidence([_offer(is_package=True)]), {})
    assert plan["status"] == "ready"
    assert _operation(plan, "promo_offer_master", 4)["values"]["is_package"] is True
    assert _operation(plan, "promo_offer_items", 5)["values"] == {"unit_price": None}


def test_total_unit_price_rounds_to_four_places_for_live_trigger():
    offer = _offer(offer_raw_text="Botox 50 units Regular $450 Special $399.99", discount_price=399.99)
    plan = build_plan(SOURCE, _graph(), _evidence([offer]), {})
    assert _operation(plan, "promo_offer_items", 5)["values"]["unit_price"] == 7.9998


def test_new_rows_use_negative_ids_and_negative_parent_references():
    graph = _graph()
    graph.update(promotions=[], offers=[], items=[])
    plan = build_plan(SOURCE, graph, _evidence(promotion_end_date="2026-10-31"), {})
    assert plan["status"] == "ready"
    assert [op["id"] for op in plan["operations"]] == [-1, -1, -1]
    assert plan["operations"][1]["values"]["promotion_id"] == -1
    assert plan["operations"][2]["values"]["offer_id"] == -1
    assert all(op["action"] == "insert" for op in plan["operations"])


def test_blanket_offer_clears_specific_service_without_rewriting_text():
    raw = "Special 20% off all services"
    offer = _offer(offer_raw_text=raw, regular_price=None, discount_price=None, discount_percent=20,
                   items=[{"item_name": "All services", "service_id": 12, "delivered_units": None}])
    graph = _graph()
    graph["offers"][0].update({k: v for k, v in offer.items() if k in graph["offers"][0]})
    graph["promotions"][0]["promotion_content"] = [raw]
    graph["items"][0].update(delivered_units=None, unit_price=None)
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "promo_offer_items", 5)["values"]["service_id"] is None
    assert all(op["action"] == "update" for op in plan["operations"])
    assert not any("offer_raw_text" in op["values"] for op in plan["operations"])


@pytest.mark.parametrize("change", [
    {"service_id": 999}, {"service_name": "Dysport"}, {"service_id": None, "service_name": "Unknown"}
])
def test_unknown_concrete_service_keeps_evidenced_item_with_null_mapping(change):
    offer = _offer(items=[{**_offer()["items"][0], **change}])
    graph = _graph()
    graph["offers"] = []
    graph["items"] = []
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    assert plan["notes"][0]["reason"] == "service_mapping_pending"
    assert not plan["review"]
    assert any(op["table"] == "promo_offer_items" and op["values"].get("service_id", 1) is None for op in plan["operations"])


def test_same_name_multiple_service_areas_preserves_unique_existing_item():
    graph = _graph()
    graph["services"] += [{"service_id": 13, "business_id": 7, "service_name": "Botox", "service_area": "Face"}]
    offer = _offer(items=[{"item_name": "Botox", "delivered_units": 50}])
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    assert not any(op["action"] == "insert" for op in plan["operations"])
    assert not any(op["values"].get("service_id", 12) is None for op in plan["operations"])


def test_existing_service_dictionary_alias_resolves_only_unique_catalog_match():
    graph = _graph()
    graph["services"][0]["service_name"] = "Neurotoxin"
    offer = _offer(items=[{"item_name":"Botulinum toxin","delivered_units":50}])
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    values = _operation(plan, "promo_offer_items", 5)["values"]
    assert values["item_name"] == "Botulinum toxin" and values.get("service_id", 12) == 12
    graph["services"].append({"service_id":13,"business_id":7,"service_name":"Botulinum toxin","service_area":None})
    ambiguous = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert ambiguous["status"] == "ready", ambiguous
    assert not any(op["action"] == "insert" for op in ambiguous["operations"])


def test_ambiguous_existing_offer_match_keeps_source_in_review_without_writes():
    graph = _graph()
    graph["offers"] += [{**graph["offers"][0], "id": 6}]
    graph["items"] += [{**graph["items"][0], "offer_item_id": 8, "offer_id": 6}]
    plan = build_plan(SOURCE, graph, _evidence(), {})
    assert plan["status"] == "review" and not plan["operations"]
    assert plan["review"][-1]["reason"] == "ambiguous_offer_match"


@pytest.mark.parametrize("protection", [
    {"promotion_ids": [3]}, {"offer_ids": [4]}, {"item_ids": [5]},
    {"entries": [{"table": "promo_offer_items", "id": 5, "reason": "manual"}]}
])
def test_touched_protected_rows_and_indirect_trigger_effects_block_all_operations(protection):
    plan = build_plan(SOURCE, _graph(), _evidence([_offer(is_package=True)]), protection)
    assert plan["status"] == "review" and plan["operations"] == []
    assert plan["review"][-1]["reason"] == "protected_row_or_cascade"


def _with_extra_offer(graph):
    graph["services"] += [{"service_id": 13, "business_id": 7, "service_name": "Dysport"}]
    graph["offers"] += [{**graph["offers"][0], "id": 6, "offer_raw_text": "Dysport Special $200"}]
    graph["items"] += [{"offer_item_id": 8, "offer_id": 6, "service_id": 13,
                        "item_name": "Dysport", "delivered_units": None, "unit_price": None}]
    return graph


def test_complete_nonempty_evidence_deactivates_missing_offer():
    plan = build_plan(SOURCE, _with_extra_offer(_graph()), _evidence(), {})
    assert plan["status"] == "ready"
    assert _operation(plan, "promo_offer_master", 6)["values"] == {"is_active": False}
    assert not any(op["action"] == "delete" for op in plan["operations"])


@pytest.mark.parametrize("image", [False, True])
def test_partial_or_unresolved_image_evidence_can_update_but_cannot_remove(image):
    offers = [_offer(offer_raw_text=RAW.replace("$350", "$400"), discount_price=400)]
    if image:
        offers += [_offer(visibility="image_only")]
    evidence = _evidence(offers, complete=image)
    plan = build_plan(SOURCE, _with_extra_offer(_graph()), evidence, {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "promo_offer_master", 4)["values"]["discount_price"] == 400
    assert not any(op["values"].get("is_active") is False for op in plan["operations"])
    assert _operation(plan, "clinic_promotions", 3)["values"]["is_offer_extracted"] is False
    assert plan["evidence_summary"]["complete"] is False


@pytest.mark.parametrize("changes", [{"offers": []}, {"markdown": ""}, {"accessible": False},
                                      {"offers": [_offer(visibility="image_only")]}])
def test_empty_error_and_image_only_sources_do_not_deactivate(changes):
    plan = build_plan(SOURCE, _graph(), _evidence(**changes), {})
    assert plan["status"] == "review" and plan["operations"] == []


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


def test_partial_extraction_retains_unprocessed_promotion_content():
    graph = _graph()
    graph["promotions"][0]["promotion_content"].append("Unprocessed image-only facial offer")
    offer = _offer(offer_raw_text="Botox 50 units Regular $450 Special $400", discount_price=400)
    plan = build_plan(SOURCE, graph, _evidence([offer], complete=False), {})
    content = _operation(plan, "clinic_promotions", 3)["values"]["promotion_content"]
    assert content == [offer["offer_raw_text"], "Unprocessed image-only facial offer"]


def test_nonpackage_total_without_delivered_quantity_uses_trigger_coalesce_one():
    offer = _offer(offer_raw_text="Botox Regular $450 Special $350",
                   items=[{"item_name": "Botox", "service_id": 12}])
    plan = build_plan(SOURCE, _graph(), _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    values = _operation(plan, "promo_offer_items", 5)["values"]
    assert values["delivered_units"] is None and values["unit_price"] == 350


def test_duplicate_new_evidence_offer_is_not_inserted_twice():
    graph = _graph()
    graph.update(promotions=[], offers=[], items=[])
    plan = build_plan(SOURCE, graph, _evidence([_offer(), _offer()], promotion_end_date="2026-10-31"), {})
    assert plan["status"] == "review" and not plan["operations"]
    assert plan["review"][-1]["reason"] == "duplicate_evidence_offer"


def test_http_www_alias_preserves_existing_source_url_on_update():
    graph = _graph()
    graph["promotions"][0]["source_url"] = "http://www.clinic.example/specials/"
    offer = _offer(offer_raw_text=RAW.replace("$350", "$400"), discount_price=400)
    plan = build_plan(SOURCE, graph, _evidence([offer], url="https://www.clinic.example/specials"), {})
    assert plan["status"] == "ready", plan
    assert "source_url" not in _operation(plan, "clinic_promotions", 3)["values"]


def test_promotion_content_is_validated_quote_array_not_full_page_markdown():
    offer = _offer(offer_raw_text=RAW.replace("$350", "$400"), discount_price=400)
    markdown = "Hidden old regular service menu $999\n" + offer["offer_raw_text"] + "\nContact us"
    plan = build_plan(SOURCE, _graph(), _evidence([offer], markdown=markdown), {})
    assert plan["status"] == "ready"
    assert _operation(plan, "clinic_promotions", 3)["values"]["promotion_content"] == [offer["offer_raw_text"]]


def test_multiple_validated_offers_persist_all_quotes_in_same_campaign():
    graph = _with_extra_offer(_graph())
    dysport = _offer(offer_raw_text="Dysport Regular $300 Special $200", regular_price=300, discount_price=200,
                     items=[{"item_name": "Dysport", "service_id": 13}])
    plan = build_plan(SOURCE, graph, _evidence([_offer(), dysport]), {})
    assert plan["status"] == "ready", plan
    assert _operation(plan, "clinic_promotions", 3)["values"]["promotion_content"] == [RAW, dysport["offer_raw_text"]]


def test_verified_complete_ocr_offer_may_update_prices():
    offer = _offer(offer_raw_text=RAW.replace("$350", "$400"), discount_price=400, visibility="image_only")
    plan = build_plan(SOURCE, _graph(), _evidence([offer], image_review_complete=True), {})
    assert plan["status"] == "ready", plan
    assert plan["evidence_summary"]["complete"] is True
    assert _operation(plan, "promo_offer_master", 4)["values"]["discount_price"] == 400


def test_blanket_scope_change_is_a_new_offer_not_price_only_rewrite():
    graph = _graph()
    graph["offers"][0].update(offer_raw_text="Special 20% off all services", regular_price=None,
                              discount_price=None, discount_percent=20)
    graph["items"][0].update(service_id=None, item_name="All services", delivered_units=None, unit_price=None)
    offer = _offer(offer_raw_text="Special 20% off all products", regular_price=None, discount_price=None,
                   discount_percent=20, items=[{"item_name": "All products"}])
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    assert any(op["table"] == "promo_offer_master" and op["action"] == "insert" for op in plan["operations"])
    assert _operation(plan, "promo_offer_master", 4)["values"] == {"is_active": False}
    assert _operation(plan, "clinic_promotions", 3)["values"]["is_offer_extracted"] is False


def test_others_placeholder_resolves_exact_non_injectable_service():
    graph = _graph()
    graph["services"][0]["service_name"] = "Hydrafacial"
    offer = _offer(offer_raw_text="Hydrafacial Regular $450 Special $350",
                   items=[{"service_name":"Others","item_name":"Hydrafacial","quantity":None,"unit_price":None}])
    plan = build_plan(SOURCE,graph,_evidence([offer]),{})
    assert plan["status"] == "ready", plan
    changes = _operation(plan,"promo_offer_items",5)["values"]
    assert changes.get("service_id",graph["items"][0]["service_id"]) == 12


def test_percent_only_per_unit_offer_preserves_explicit_source_rate():
    offer = _offer(offer_raw_text="Botox Special $8.99/unit, 20% off", regular_price=None,discount_price=None,
        discount_percent=20,price_model="per_unit",quantity_kind="variable",
        items=[{"item_name":"Botox","service_id":12,"delivered_units":None,"unit_price":8.99,"unit_type":"unit"}])
    plan = build_plan(SOURCE,_graph(),_evidence([offer]),{})
    assert plan["status"] == "ready",plan
    assert _operation(plan,"promo_offer_master",4)["values"]["discount_price"] is None
    assert _operation(plan,"promo_offer_items",5)["values"]["unit_price"] == 8.99


@pytest.mark.parametrize("quantity",[1.5,32768])
def test_unrepresentable_fixed_quantity_requires_review(quantity):
    offer=_offer(items=[{"item_name":"Botox","service_id":12,"delivered_units":quantity}])
    plan=build_plan(SOURCE,_graph(),_evidence([offer]),{})
    assert plan["status"] == "review" and not plan["operations"]


def test_legacy_null_conditions_match_empty_current_conditions_preserving_id():
    graph=_graph();graph["offers"][0]["conditions"]=None
    offer=_offer(offer_raw_text=RAW.replace("$350","$400"),discount_price=400)
    plan=build_plan(SOURCE,graph,_evidence([offer]),{})
    assert plan["status"] == "ready",plan
    assert all(op["action"] == "update" for op in plan["operations"])
    assert _operation(plan,"promo_offer_master",4)["values"]["discount_price"] == 400


def test_empty_page_without_live_rows_is_a_noop_not_review():
    graph = _graph()
    graph["promotions"][0]["is_active"] = False
    graph["offers"][0]["is_active"] = False
    evidence = _evidence([], complete=False, markdown="Botox treatment information")
    plan = build_plan(SOURCE, graph, evidence, {})
    assert plan["status"] == "ready" and plan["operations"] == [], plan
    assert plan["notes"][-1]["reason"] == "no_offers_and_no_live_rows"


def test_empty_page_with_live_rows_still_requires_review():
    evidence = _evidence([], complete=False, markdown="Botox treatment information")
    plan = build_plan(SOURCE, _graph(), evidence, {})
    assert plan["status"] == "review"
    assert plan["review"][-1]["reason"] == "inaccessible_empty_or_unresolved_evidence"


def test_unsupported_item_quantity_is_written_as_null_not_reviewed():
    raw = "Botox Regular $450 Special $350"
    offer = _offer(offer_raw_text=raw, items=[{"item_name": "Botox", "service_id": 12, "delivered_units": 50}])
    graph = _graph()
    graph["offers"][0]["offer_raw_text"] = raw
    plan = build_plan(SOURCE, graph, _evidence([offer]), {})
    assert plan["status"] == "ready", plan
    assert not any(r["reason"] == "unsupported_scope_or_quantity" for r in plan["review"])
    assert _operation(plan, "promo_offer_items", 5)["values"]["delivered_units"] is None


def test_unsupported_new_customer_flag_is_cleared_only_when_page_is_silent():
    offer = _offer(is_new_customer_required=True)
    silent = build_plan(SOURCE, _graph(), _evidence([offer]), {})
    assert silent["status"] == "ready" and silent["operations"] == [], silent
    mentioned = build_plan(SOURCE, _graph(), _evidence([offer], markdown=RAW + "\nNew patient special"), {})
    assert any(r["reason"] == "unsupported_scope_or_quantity" for r in mentioned["review"]), mentioned


def test_variant_reconciliation_mismatch_blocks_write():
    variant = _offer(
        offer_raw_text="Naked TOX\nMember\n10 Units TOX Package\n$69.90",
        discount_price=69.9,
        regular_price=79.9,
        is_membership_required=True,
        conditions=["Member", "10 Units TOX Package"],
        items=[{"item_name": "Naked TOX", "service_id": 12, "delivered_units": 10, "unit_price": None}],
        purchase_variant=True,
    )
    evidence = _evidence([variant], product_variant_reconciliation=[{
        "variant_expected": 2, "variant_skipped": [], "variant_capture_count": 1}])
    plan = build_plan(SOURCE, _graph(), evidence, {})
    assert any(r["reason"] == "variant_reconciliation_mismatch" for r in plan["review"]), plan


def test_variant_units_one_triggers_reconciliation_review():
    variant = _offer(
        offer_raw_text="Naked TOX\nMember\n10 Units TOX Package\n$69.90",
        discount_price=69.9,
        is_membership_required=True,
        conditions=["Member", "10 Units TOX Package"],
        items=[{"item_name": "Naked TOX", "service_id": 12, "delivered_units": 1, "unit_price": None}],
        purchase_variant=True,
    )
    evidence = _evidence([variant], product_variant_reconciliation=[{
        "variant_expected": 1, "variant_skipped": [], "variant_capture_count": 1}])
    plan = build_plan(SOURCE, _graph(), evidence, {})
    assert any(r["reason"] == "variant_reconciliation_mismatch" for r in plan["review"]), plan


def test_regular_price_lines_are_ignored_not_reviewed():
    price_list = _offer(offer_raw_text="Botox $12 per unit", regular_price=None, discount_price=12,
                        price_model="per_unit", items=[{"item_name": "Botox", "service_id": 12}])
    graph = _graph()
    graph["promotions"][0]["is_active"] = False
    graph["offers"][0]["is_active"] = False
    plan = build_plan(SOURCE, graph, _evidence([price_list]), {})
    assert plan["status"] == "ready" and plan["operations"] == [], plan
    assert [n["reason"] for n in plan["notes"]][-1] == "no_offers_and_no_live_rows"
