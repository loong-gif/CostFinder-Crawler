"""Focused offline contracts for source-scoped price synchronization plans."""
from copy import deepcopy

import pytest

from monitor_price_plan_support import (
    RAW, SOURCE, _changed_condition_offer, _evidence, _graph, _offer, _operation, _with_extra_offer,
)
from utils.monitor_price_plan import build_plan


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
    assert all("is_offer_extracted" not in op["values"] for op in plan["operations"]
               if op["table"] == "clinic_promotions")


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
    assert all("is_offer_extracted" not in op["values"] for op in plan["operations"]
               if op["table"] == "clinic_promotions")
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
    assert all("is_offer_extracted" not in op["values"] for op in plan["operations"]
               if op["table"] == "clinic_promotions")
    assert plan["evidence_summary"]["complete"] is False


@pytest.mark.parametrize("changes", [{"offers": []}, {"markdown": ""}, {"accessible": False},
                                      {"offers": [_offer(visibility="image_only")]}])
def test_empty_error_and_image_only_sources_do_not_deactivate(changes):
    plan = build_plan(SOURCE, _graph(), _evidence(**changes), {})
    assert plan["status"] == "review" and plan["operations"] == []
