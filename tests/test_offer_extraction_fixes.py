from utils.offer_evidence_validator import validate_offer_evidence
from utils.offer_purchase_semantics import normalize_purchase_offers
from utils.offer_schema import base_offer_schema
from utils.offer_scope_filter import (
    ITEM_NAME_MAX_LENGTH, item_name_problem, multiple_services_reason, non_promotable_reason, short_item_label)
from utils.offer_stage_extract import candidates_from_payload
from utils.offer_stage_reconcile import plan_offers, plan_source
from utils.offer_stage_spec import MASTER_COLUMNS, MEMBERSHIPS, PROMOTIONS, source_lines
from utils.offer_stage_sql import _verify_fresh_statements, render_apply


def offer(raw, **fields):
    base = {"purchase_type": "single", "is_package": False, "price_model": "total", "regular_price": None,
            "discount_price": None, "discount_percent": None, "discount_amount": None, "min_quantity": None,
            "is_membership_required": False, "is_new_customer_required": False, "conditions": [],
            "offer_raw_text": raw,
            "items": [{"item_name": "Item", "service_name": None, "quantity": None, "unit_price": None,
                       "unit_type": None, "service_area": None}]}
    return {**base, **fields}


def test_bundle_items_do_not_split_inside_thousands_separators():
    raw = "Morpheus8 Face & Neck: 1 session $700 (reg. $1,400) + 1 complimentary Evolve Transform"
    offers, reviews = normalize_purchase_offers([offer(raw, is_package=True, purchase_type="bundle",
                                                       regular_price=1400, discount_price=700)], raw)
    names = [item["item_name"] for o in offers for item in o["items"]]
    assert reviews == [] and not any("$" in n or n.endswith(")") for n in names)
    assert len(names) == 2


def test_shared_price_choice_keeps_the_for_connector_with_the_price():
    raw = "Xeomin/Jeuveau for $9.00 per unit"
    offers, _ = normalize_purchase_offers([offer(raw, price_model="per_unit", discount_price=9.0)], raw)
    assert [o["offer_raw_text"] for o in offers] == ["Xeomin | for $9.00 per unit", "Jeuveau | for $9.00 per unit"]
    assert all(validate_offer_evidence(o, raw).accepted for o in offers)


def test_percent_savings_and_percent_word_are_discount_evidence():
    for raw, percent in (("15% skincare savings", 15), ("Receive 10 percent off most products and services", 10)):
        decision = validate_offer_evidence(offer(raw, discount_percent=percent), raw)
        assert decision.accepted and decision.offer["discount_percent"] == percent


def test_free_add_on_with_package_price_is_a_promotion():
    raw = "3 sessions of Microneedling with PRF + FREE neck add-on — $1,950"
    assert validate_offer_evidence(offer(raw, discount_price=1950), raw).accepted


def test_member_unit_price_in_a_membership_benefit_list_is_a_member_price():
    raw = "Botox | $14/unit"
    per_unit = offer(raw, price_model="per_unit", discount_price=14)
    assert not validate_offer_evidence(per_unit, raw).accepted
    assert validate_offer_evidence(per_unit, raw, member_context=True).accepted


def test_composite_membership_line_is_split_into_unit_prices_and_a_discount():
    line = "2 service credits monthly; Botox/Xeomin $14/unit; 10% off skincare."
    row = {"plan_id": 857, "business_id": 4050, "source_url": "https://a.test/m", "membership_name": "Gold",
           "membership_price": 99, "billing_period": "monthly", "benefits": [line], "updated_at": None}
    payload = {"offers": [
        offer("Botox/Xeomin $14/unit", price_model="per_unit", discount_price=14,
              items=[{"item_name": "Botox", "service_name": "Botox", "quantity": None, "unit_price": 14,
                      "unit_type": "unit", "service_area": None},
                     {"item_name": "Xeomin", "service_name": "Xeomin", "quantity": None, "unit_price": 14,
                      "unit_type": "unit", "service_area": None}]),
        offer("10% off skincare.", discount_percent=10)]}
    candidates, rejected = candidates_from_payload(payload, MEMBERSHIPS, row, line)
    assert sorted(c["master"]["offer_raw_text"] for c in candidates) == [
        "10% off skincare.", "Botox | $14/unit", "Xeomin | $14/unit"], rejected


def test_one_price_over_several_services_becomes_independent_offers():
    raw = "Botox/Dysport $12/unit"
    row = {"plan_id": 5, "business_id": 1, "source_url": "https://a.test/m", "membership_name": "Gold",
           "membership_price": 99, "billing_period": "monthly", "benefits": [raw], "updated_at": None}
    both = offer(raw, price_model="per_unit", discount_price=12,
                 items=[{"item_name": name, "service_name": name, "quantity": None, "unit_price": 12,
                         "unit_type": "unit", "service_area": None} for name in ("Botox", "Dysport")])
    candidates, _ = candidates_from_payload({"offers": [both]}, MEMBERSHIPS, row, raw)
    assert sorted(c["master"]["offer_raw_text"] for c in candidates) == ["Botox | $12/unit", "Dysport | $12/unit"]
    assert all(len(c["items"]) == 1 for c in candidates)


def test_composite_offer_with_several_services_is_rejected_unless_it_is_a_package():
    raw = "Botox, Dysport and Xeomin"
    assert multiple_services_reason({"offer_raw_text": raw}) == "multiple_services_in_offer"
    assert multiple_services_reason({"offer_raw_text": raw, "is_package": True}) == ""
    assert multiple_services_reason({"offer_raw_text": "Botox $12/unit"}) == ""
    assert item_name_problem("Botox/Dysport") == "multiple_services"
    assert item_name_problem("Botox") == ""


def test_semicolon_clauses_are_separate_source_lines():
    row = {"plan_id": 5, "benefits": ["1 credit monthly; Botox/Xeomin $14/unit; 10% off skincare (members only; yearly)."]}
    assert source_lines(MEMBERSHIPS, row) == ["1 credit monthly", "Botox/Xeomin $14/unit",
                                              "10% off skincare (members only; yearly)."]


def test_old_composite_offer_is_deactivated_when_it_is_split_into_independent_offers():
    old = {column: None for column in MASTER_COLUMNS}
    old.update(id=9, business_id=1, membership_plan_id=5, is_active=True, updated_at=None,
               offer_raw_text="Botox, Xeomin, Dysport $14/unit", price_model="per_unit", discount_price=14.0,
               is_package=False, is_membership_required=True, is_new_customer_required=False, conditions=[])
    def candidate_for(text):
        master = {column: None for column in MASTER_COLUMNS}
        master.update(business_id=1, membership_plan_id=5, offer_raw_text=text, price_model="per_unit",
                      discount_price=14.0, is_package=False, is_membership_required=True,
                      is_new_customer_required=False, conditions=[])
        return {"fingerprint": f"m:5|{text}", "master": master, "blanket": False,
                "items": [{"item_name": text.split(" | ")[0], "delivered_units": None, "unit_price": 14.0,
                           "service_name_hint": None}]}
    actions, _ = plan_offers([candidate_for("Botox | $14/unit"), candidate_for("Xeomin | $14/unit")], [old], {9: []},
                             set(), "Botox, Xeomin, Dysport $14/unit")
    assert [a["action"] for a in actions] == ["insert", "insert", "deactivate"] and actions[-1]["offer_id"] == 9


def test_plain_package_price_is_not_a_promotion():
    raw = "3 sessions of PRF Hair — $1,950"
    decision = validate_offer_evidence(offer(raw, discount_price=1950), raw)
    assert not decision.accepted and decision.reason == "no_supported_promo_fact"


def insert_action(price_model, service_id):
    master = {column: None for column in MASTER_COLUMNS}
    master.update(business_id=1, promotion_id=7, offer_raw_text="Botox $10 per unit", price_model=price_model,
                  discount_price=10.0, is_package=False, is_membership_required=False,
                  is_new_customer_required=False, conditions=[])
    item = {"action": "insert", "item_name": "Botox", "service_id": service_id, "delivered_units": None,
            "unit_price": 10.0}
    return {"action": "insert", "fingerprint": "p:7|botox", "master": master, "blanket": False, "is_active": True,
            "items": [item]}


def render_source(action):
    source = {"table": "clinic_promotions", "id": 7, "status": "ready", "source_updated_at": "2026-01-01T00:00:00+00:00",
              "existing_state": {}, "existing_item_ids": {}, "offers": [action]}
    return render_apply({"sources": [source]})


def test_unlinked_per_unit_offer_is_inserted_inactive_and_not_expected_active():
    sql = render_source(insert_action("per_unit", None))
    assert "set is_active=true" not in sql and "v_new := v_new || v_offer" not in sql


def test_linked_per_unit_offer_and_total_offer_are_activated():
    assert "set is_active=true" in render_source(insert_action("per_unit", 55))
    assert "set is_active=true" in render_source(insert_action("total", None))


def candidate(raw, item_names=("Item",)):
    return {"offer_raw_text": raw, "items": [{"item_name": name} for name in item_names]}


def test_flattened_fragments_are_not_promotable():
    for raw, names in (
        ("10% off | and already discounted packages", ("and already discounted packages",)),
        ("15% off | and lab work (no limits | ; must be ordered / performed at Renew)", ("and lab work (no limits",)),
        ("15% off | peptides | ; must be ordered / performed at Renew)", ("peptides",)),
    ):
        assert non_promotable_reason(candidate(raw, names)) == "fragment_offer_text"


def test_membership_term_gift_card_and_event_discounts_are_not_promotable():
    assert non_promotable_reason(candidate(
        "Purchase 12 months upfront and receive 3 additional months free (20% savings)", ("Membership",))
    ) == "membership_term_offer"
    assert non_promotable_reason(candidate(
        "Buy one Ultra Smooth Skin Gift card for $75 towards Botox, Get $75 FREE")) == "gift_card_offer"
    assert non_promotable_reason(candidate("15% off | member events | .", ("member events",))) == "non_service_target"


def test_item_names_about_members_events_or_gift_cards_are_not_promotable():
    for name in ("member events", "Member 10 Units TOX", "Non-Member 50 Units TOX", "VIP Membership",
                 "10% off all services during Membership period", "Core Members 20% off",
                 "VIP event invitations, member-only offers and 15% off skincare/products.",
                 "Holiday gift cards as the perfect stocking stuffer"):
        assert non_promotable_reason(candidate("15% off services", (name,))) == "non_service_target", name


def test_ordinary_service_discounts_stay_promotable():
    for raw, names in (
        ("10% off | treatments", ("treatments",)),
        ("10% off | services (including injectables | ; excluding neurotoxins)", ("services (including injectables",)),
        ("Receive 10 percent off most products and services (excludes packages, already discounted items)", ("x",)),
        ("15% off | additional aesthetic services | .", ("additional aesthetic services",)),
        ("2) $65 off your first BOTOX® Cosmetic treatment", ("BOTOX® Cosmetic",)),
    ):
        assert non_promotable_reason(candidate(raw, names)) == ""


def test_short_item_label_is_bounded_and_never_ends_inside_a_parenthesis():
    raw = "Receive 10 percent off most products and services (excludes packages, already discounted items)"
    label = short_item_label(raw)
    assert 0 < len(label) <= ITEM_NAME_MAX_LENGTH and label.count("(") == label.count(")")
    assert short_item_label("15% off products & services") == "15% off products & services"


def test_item_name_defined_in_schema_has_the_length_limit():
    item = base_offer_schema()["properties"]["offers"]["items"]["properties"]["items"]["items"]["properties"]["item_name"]
    assert item["maxLength"] == ITEM_NAME_MAX_LENGTH


def test_overlong_llm_item_name_rejects_the_offer_and_blanket_name_is_shortened():
    long_name = "Laser Hair Removal for the full body including underarms legs bikini and back areas"
    raw = "Laser Hair Removal full body just $99"
    llm = offer(raw, discount_price=99, items=[{"item_name": long_name, "service_name": None, "quantity": None,
                                                 "unit_price": None, "unit_type": None, "service_area": None}])
    row = {"promotion_id": 7, "business_id": 3, "source_url": "https://a.test/specials", "promotion_title": "t",
           "promotion_content": [raw], "is_active": True, "updated_at": None}
    candidates, rejected = candidates_from_payload({"offers": [llm]}, PROMOTIONS, row, raw)
    assert candidates == [] and [r["reason"] for r in rejected] == ["item_name_invalid"]

    blanket_raw = "Receive 10 percent off most products and services (excludes packages, already discounted items)"
    row["promotion_content"] = [blanket_raw]
    candidates, _ = candidates_from_payload({"offers": [offer(blanket_raw, discount_percent=10)]}, PROMOTIONS, row,
                                            blanket_raw)
    assert len(candidates[0]["items"][0]["item_name"]) <= ITEM_NAME_MAX_LENGTH


def test_blanket_offer_keeps_the_valid_item_name_given_by_the_llm():
    raw = "Up to 25% Off 3+ Skincare Products"
    llm = offer(raw, discount_percent=25, items=[{"item_name": "Skincare Products", "service_name": None,
                                                   "quantity": None, "unit_price": None, "unit_type": None,
                                                   "service_area": None}])
    row = {"promotion_id": 7, "business_id": 3, "source_url": "https://a.test/specials", "promotion_title": "t",
           "promotion_content": [raw], "is_active": True, "updated_at": None}
    candidates, _ = candidates_from_payload({"offers": [llm]}, PROMOTIONS, row, raw)
    assert candidates[0]["blanket"] is True
    assert [item["item_name"] for item in candidates[0]["items"]] == ["Skincare Products"]


LASERAWAY_CARDS = [
    "Your First Botox Treatment Is $10 Per Unit\nOur #1 wrinkle relaxer for a smooth, youthful look.\n"
    "Botox New Patient Promotion ‡‡Valid on purchases made by phone or in person. Receive your first Botox purchase "
    "of 20 to 300 units for $10 per unit (residents of NV can only purchase in batches of 50). Available only to new "
    "patients or existing patients who have never purchased Botox at LaserAway. Limit 1 per patient.",
    "40% Off Your First Clear + Brilliant Laser Facial\nImprove texture and tone.\nClear + Brilliant® New Patient "
    "Promotion ✢Available only to new patients. Valid only on Clear + Brilliant treatments for the Face area.",
    "$90 Instant Glow Laser Facial\nOur 'lunchtime facial' for a filtered look in minutes.\nInstant Glow New Patient "
    "Promotion ◊§Available only to new patients or existing patients who have never purchased Instant Glow "
    "treatments at LaserAway. Valid only on Instant Glow Laser Facials for the Face area.",
]


def _item(name, service, **fields):
    return {"item_name": name, "service_name": service, "quantity": None, "unit_price": None, "unit_type": None,
            "service_area": None, **fields}


def test_card_terms_support_new_patient_minimum_quantity_and_first_purchase_price():
    row = {"promotion_id": 7, "business_id": 3, "source_url": "https://laseraway.com/promotions",
           "promotion_title": "t", "promotion_content": LASERAWAY_CARDS, "is_active": True, "updated_at": None}
    llm = {"offers": [
        offer("Your First Botox Treatment Is $10 Per Unit", price_model="per_unit", discount_price=10, min_quantity=20,
              is_new_customer_required=True,
              items=[_item("Botox", "Botox", unit_price=10, unit_type="unit")]),
        offer("40% Off Your First Clear + Brilliant Laser Facial", price_model="from", discount_percent=40,
              is_new_customer_required=True,
              items=[_item("Clear + Brilliant Laser Facial", "Clear + Brilliant", service_area="Face")]),
        offer("$90 Instant Glow Laser Facial", discount_price=90, is_new_customer_required=True,
              items=[_item("Instant Glow Laser Facial", "Facial", service_area="Face")]),
    ]}
    candidates, rejected = candidates_from_payload(llm, PROMOTIONS, row, "\n".join(LASERAWAY_CARDS))
    assert rejected == []
    masters = {c["master"]["offer_raw_text"]: c["master"] for c in candidates}
    botox = masters["Your First Botox Treatment Is $10 Per Unit"]
    assert botox["discount_price"] == 10 and botox["is_new_customer_required"] is True
    # 条件取自卡片原文的条件句（去掉条款标题与脚注符号），LLM 挑选的条件不影响结果
    assert botox["conditions"] == [
        "Valid on purchases made by phone or in person.",
        "Receive your first Botox purchase of 20 to 300 units for $10 per unit (residents of NV can only purchase "
        "in batches of 50).",
        "Available only to new patients or existing patients who have never purchased Botox at LaserAway.",
        "Limit 1 per patient."]
    # 「Clear + Brilliant」是一个服务名，不拆成组合
    facial = next(c for c in candidates if "Clear + Brilliant" in c["master"]["offer_raw_text"])
    assert facial["master"]["is_package"] is False
    assert [i["item_name"] for i in facial["items"]] == ["Clear + Brilliant Laser Facial"]
    # 价格只写在标题里，促销性质（New Patient Promotion）写在卡片的条款段落里
    glow = masters["$90 Instant Glow Laser Facial"]
    assert glow["discount_price"] == 90 and glow["is_new_customer_required"] is True


def test_quantity_plus_is_not_a_bundle_and_regular_volume_pricing_is_not_a_promotion():
    raw = "50% Savings On CoolSculpting (8+ Treatments)"
    llm = offer(raw, price_model="from", discount_percent=50, min_quantity=8,
                items=[_item("CoolSculpting", "CoolSculpting", quantity=8, unit_type="treatment")])
    offers, reviews = normalize_purchase_offers([llm], raw)
    assert reviews == [] and [[i["item_name"] for i in o["items"]] for o in offers] == [["CoolSculpting"]]
    assert offers[0]["is_package"] is False

    promo = {"promotion_id": 7, "business_id": 3, "source_url": "https://a.test/specials", "promotion_title": "t",
             "is_active": True, "updated_at": None}
    volume = f"{raw}\nCoolSculpting Volume Pricing ✴✴Pricing reflects our regular volume price. Stated savings show " \
             "the value compared to the price if each included treatment was purchased individually."
    candidates, rejected = candidates_from_payload({"offers": [llm, llm]}, PROMOTIONS,
                                                   {**promo, "promotion_content": [volume]}, volume)
    assert candidates == [] and {r["reason"] for r in rejected} == {"regular_price_not_promotion"}
    assert not any(r["blocking"] for r in rejected)
    # 没有常规价格说明时是 50% 的批量折扣；LLM 重复给出同一条 offer 不阻塞
    candidates, rejected = candidates_from_payload({"offers": [llm, llm]}, PROMOTIONS,
                                                   {**promo, "promotion_content": [raw]}, raw)
    assert [c["master"]["discount_percent"] for c in candidates] == [50]
    assert [(r["reason"], r["blocking"]) for r in rejected] == [("duplicate_in_source", False)]


def test_raw_text_spanning_a_trailing_note_line_is_cut_after_the_priced_line():
    card = ("The Black Friday Sale\n75% OFF LASER HAIR REMOVAL\nSelect packages.\nThe Small Area Laser Hair Removal "
            "Package Promotion ǂValid on purchases made November 3, 2026 to December 7, 2026 by phone or in person.")
    row = {"promotion_id": 7, "business_id": 3, "source_url": "https://laseraway.com/promotions",
           "promotion_title": "t", "promotion_content": [card], "is_active": True, "updated_at": None}
    masters = []
    for raw in ("75% OFF LASER HAIR REMOVAL", "75% OFF LASER HAIR REMOVAL Select packages."):
        llm = offer(raw, price_model="from", discount_percent=75,
                    items=[_item("Laser Hair Removal", "Laser Hair Removal")])
        candidates, _ = candidates_from_payload({"offers": [llm]}, PROMOTIONS, row, card)
        masters.append((candidates[0]["fingerprint"], candidates[0]["master"]["offer_raw_text"]))
    # LLM 取不取说明行，offer 原文与指纹都相同
    assert masters[0] == masters[1] and masters[0][1] == "75% OFF LASER HAIR REMOVAL"


def test_card_conditions_are_not_borrowed_from_another_card():
    cards = ["Hydrafacial $99\nThis month only", "Botox $11 per unit\nNew patients only"]
    row = {"promotion_id": 7, "business_id": 3, "source_url": "https://a.test/specials", "promotion_title": "t",
           "promotion_content": cards, "is_active": True, "updated_at": None}
    decision = validate_offer_evidence(offer("Hydrafacial $99", discount_price=99, is_new_customer_required=True),
                                       "\n".join(cards), scope_text=cards[0])
    assert decision.offer["is_new_customer_required"] is False


def test_source_with_null_updated_at_is_planned_and_checked():
    row = {"plan_id": 5, "business_id": 1, "source_url": "https://clinic.test/m", "membership_name": "Gold",
           "membership_price": 99, "billing_period": "monthly", "benefits": ["10% off"], "updated_at": None}
    plan = plan_source(MEMBERSHIPS, row, {"offers": []}, "10% off", [], {}, set())
    assert plan["source_updated_at"] is None
    sql = " ".join(_verify_fresh_statements(plan))
    assert "updated_at from public.clinic_memberships where plan_id = 5) is not null" in sql
