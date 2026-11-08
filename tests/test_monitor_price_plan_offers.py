import pytest

from monitor_price_plan_support import RAW, SOURCE, _evidence, _graph, _offer, _operation, _with_extra_offer
from utils.monitor_price_plan import build_plan


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
    assert all("is_offer_extracted" not in op["values"] for op in plan["operations"]
               if op["table"] == "clinic_promotions")


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
