from copy import deepcopy

from utils.offer_evidence_validator import validate_offer_evidence
from utils.offer_purchase_semantics import (
    canonical_offer_semantics, normalize_purchase_offers, offer_update_identity,
    service_supported_by_source, usable_unit_price,
)


def _offer(raw, **changes):
    return {"offer_raw_text": raw, "regular_price": None, "discount_price": 99,
            "discount_percent": None, "discount_amount": None, "conditions": [],
            "is_membership_required": False, "is_new_customer_required": False,
            "purchase_type": "single", "is_package": False,
            "items": [{"item_name": "Lip Flip", "service_name": "Others", "quantity": None}], **changes}


def test_or_choice_shares_price_and_gift_without_gift_item_or_other_option_text():
    raw = "Lip Flip or Brow Lift $99 (regular $200). Receive a free skincare sample"
    original = _offer(raw, regular_price=200, visibility="image_only",
                      items=[{"item_name": "Lip Flip"}, {"item_name": "Brow Lift"}, {"item_name": "skincare sample"}])
    before = deepcopy(original)
    offers, reviews = normalize_purchase_offers([original], raw)
    assert reviews == [] and len(offers) == 2
    for offer, option, other in zip(offers, ("Lip Flip", "Brow Lift"), ("Brow Lift", "Lip Flip")):
        assert offer["visibility"] == "image_only" and offer["source_offer_raw_text"] == raw
        assert len(offer["items"]) == 1 and offer["items"][0]["item_name"] == option
        assert other not in offer["offer_raw_text"] and "skincare sample" in offer["offer_raw_text"]
        assert validate_offer_evidence(offer, raw).accepted
    assert original == before


def test_shared_discount_brand_list_splits_even_with_and():
    for conjunction in ("or", "and"):
        raw = f"20% off Botox, Jeuveau, Dysport {conjunction} Daxxify"
        offers, reviews = normalize_purchase_offers([_offer(raw, discount_price=None, discount_percent=20)], raw)
        assert not reviews and len(offers) == 4
        assert all(len(o["items"]) == 1 and not o["is_package"] for o in offers)
        assert all(validate_offer_evidence(o, raw).accepted for o in offers)


def test_per_unit_plus_separates_shared_rate_brands():
    raw = "$11/ unit Botox + Jeuveau"
    offers, reviews = normalize_purchase_offers(
        [_offer(raw, discount_price=11, price_model="per_unit",
                items=[{"item_name": "Botox"}, {"item_name": "Botox"}])], raw)
    assert not reviews and len(offers) == 2
    assert {o["items"][0]["item_name"] for o in offers} == {"Botox", "Jeuveau"}


def test_slash_between_areas_of_one_treatment_is_not_split():
    raw = "Shoulder/Trap TOX\n$799.00\n-11%\n~~$899.00~~"
    item = {"item_name": "Shoulder/Trap TOX", "service_name": "Neurotoxin", "service_area": "Shoulder / Trap"}
    offers, reviews = normalize_purchase_offers([_offer(raw, discount_price=799, regular_price=899, items=[item])], raw)
    assert not reviews and len(offers) == 1
    assert offers[0]["offer_raw_text"] == raw and offers[0]["items"][0]["item_name"] == "Shoulder/Trap TOX"


def test_slash_between_brands_still_splits():
    raw = "Botox/Dysport $10/unit (regular $14)"
    offers, reviews = normalize_purchase_offers(
        [_offer(raw, discount_price=10, regular_price=14, price_model="per_unit",
                items=[{"item_name": "Botox/Dysport", "service_name": "Neurotoxin"}])], raw)
    assert not reviews and {o["items"][0]["item_name"] for o in offers} == {"Botox", "Dysport"}


def test_price_first_or_choice_splits():
    raw = "$99 Lip Flip or Brow Lift (regular $200)"
    offers, reviews = normalize_purchase_offers(
        [_offer(raw, regular_price=200, items=[{"item_name": "Lip Flip"}, {"item_name": "Brow Lift"}])], raw)
    assert not reviews and len(offers) == 2
    assert {o["items"][0]["item_name"] for o in offers} == {"Lip Flip", "Brow Lift"}


def test_chinese_or_choice_splits():
    raw = "Lip Flip 或 Brow Lift 优惠价 $99（原价 $200）"
    offers, reviews = normalize_purchase_offers(
        [_offer(raw, regular_price=200, discount_price=99,
                items=[{"item_name": "Lip Flip"}, {"item_name": "Brow Lift"}])], raw)
    assert not reviews and len(offers) == 2


def test_trailing_percent_off_brand_list_splits():
    raw = "Botox, Jeuveau, Dysport or Daxxify: 20% off"
    offers, reviews = normalize_purchase_offers(
        [_offer(raw, discount_percent=20, discount_price=None,
                purchase_type="independent_options",
                items=[{"item_name": n, "service_name": n} for n in
                       ("Botox", "Jeuveau", "Dysport", "Daxxify")])], raw)
    assert not reviews and len(offers) == 4


def test_pretty_girl_glow_up_bundle():
    raw = ("Pretty Girl Glow-Up: 60 Units Botox, 2 Versa Fillers, RF Microneedling + PDRN "
           "— $1,500 (Reg. $2,500)")
    items = [{"item_name": "60 Units Botox", "service_name": "Botox", "quantity": 60, "unit_type": "unit"},
               {"item_name": "2 Versa Fillers", "service_name": "Dermal Filler", "quantity": 2},
               {"item_name": "RF Microneedling", "service_name": "Others"},
               {"item_name": "PDRN", "service_name": "Others"}]
    offers, reviews = normalize_purchase_offers(
        [_offer(raw, discount_price=1500, regular_price=2500, purchase_type="bundle", is_package=True, items=items)], raw)
    assert not reviews and len(offers) == 1
    offer = offers[0]
    assert offer["is_package"] is True and offer["discount_price"] == 1500
    assert len(offer["items"]) == 4
    assert all(i.get("unit_price") is None for i in offer["items"])
    botox = next(i for i in offer["items"] if "Botox" in i["item_name"])
    assert botox.get("quantity") == 60


def test_minimum_and_eligibility_or_is_not_a_purchase_choice():
    raw = "Botox special $11/unit, 40 units or more, sign up for emails and texts"
    offers, reviews = normalize_purchase_offers([_offer(raw)], raw)
    assert not reviews and len(offers) == 1 and offers[0]["offer_raw_text"] == raw


def test_actual_buy_get_bundle_keeps_paid_and_gift_components():
    raw = "Buy 3 Morpheus8 sessions, receive 1 Forma session free"
    items = [{"item_name": "Morpheus8", "quantity": 3}, {"item_name": "Forma", "quantity": 1}]
    offers, reviews = normalize_purchase_offers([_offer(raw, items=items)], raw)
    assert not reviews and offers[0]["items"] == items
    assert offers[0]["is_package"] is True


def test_generic_favorites_never_expand_catalogue_services():
    raw = "20% off favorite treatments and products"
    offers, reviews = normalize_purchase_offers([_offer(raw, items=[{"service_name": "Botox"}])], raw)
    assert reviews[0]["reason"] == "generic_offer_scope"
    assert offers[0]["items"][0]["service_name"] is None


def test_from_model_and_uncounted_total_do_not_provide_comparable_unit_price():
    raw = "Naked TOX Special from $69.90"
    offers, _ = normalize_purchase_offers([_offer(raw, price_model="total", discount_price=69.9)], raw)
    assert offers[0]["price_model"] == "from"
    assert usable_unit_price(offers[0], {"unit_price": 69.9, "delivered_units": None}) is None
    assert usable_unit_price(_offer("Botox special $99", price_model="total"), {"unit_price": 99}) is None


def test_usable_unit_price_requires_source_rate_or_fixed_delivered_count():
    assert usable_unit_price(_offer("Botox $11/unit", price_model="per_unit", discount_price=11), {}) == 11
    assert usable_unit_price(_offer("Botox 10 units Special $99", price_model="total"), {"delivered_units": 10}) == 9.9
    assert usable_unit_price(_offer("Botox minimum 10 units Special $99", price_model="total"), {"delivered_units": 10}) is None


def test_unlabelled_larger_amount_is_not_a_regular_price():
    raw = "Botox special $99; Dermal Filler $200"
    decision = validate_offer_evidence(_offer(raw, regular_price=200), raw)
    assert decision.offer["regular_price"] is None


def test_package_sibling_items_do_not_inherit_botox_from_offer_text():
    from utils.clinic_service_extraction import infer_service_name_for_item
    raw = "Pretty Girl Glow-Up: 60 Units Botox, 2 Versa Fillers, RF Microneedling + PDRN — $1,500"
    microneedling = infer_service_name_for_item(
        offer_raw_text=raw, item_name="RF Microneedling", service_name="Others",
        sibling_count=4, is_package=True)
    pdrn = infer_service_name_for_item(
        offer_raw_text=raw, item_name="PDRN", service_name="Others",
        sibling_count=4, is_package=True)
    assert microneedling != "Botox"
    assert pdrn != "Botox"
    assert microneedling != "Dermal Filler"


def test_controlled_alias_is_evidence_but_unrelated_catalogue_mapping_is_not():
    assert service_supported_by_source("Dermal Filler", "Lip Filler special $199")
    assert not service_supported_by_source("Botox", "20% off favorite treatments")


def test_offer_update_identity_ignores_new_customer_tier():
    raw = "Jeuveau $8 per unit"
    base = _offer(raw, price_model="per_unit", discount_price=8)
    items = [{"service_id": 1, "item_name": "Jeuveau", "delivered_units": None}]
    new_key = offer_update_identity({**base, "is_new_customer_required": True}, items)
    ret_key = offer_update_identity({**base, "is_new_customer_required": False}, items)
    assert new_key == ret_key


def test_new_customer_tier_merge_patch_folds_returning_price():
    from utils.offer_purchase_semantics import new_customer_tier_merge_patch
    returning = {"is_new_customer_required": False, "discount_price": 9.0, "regular_price": None}
    new = {"is_new_customer_required": True, "discount_price": 8.0, "regular_price": None}
    assert new_customer_tier_merge_patch(returning, new) == {
        "discount_price": 8.0, "regular_price": 9.0, "is_new_customer_required": True,
    }
    assert new_customer_tier_merge_patch(new, returning) == {"regular_price": 9.0}


def test_full_equality_distinguishes_qualifications_composition_units_and_unknowns():
    base = _offer("Botox 10 units Special $99", price_model="total")
    items = [{"service_id": 1, "item_name": "Botox", "delivered_units": 10}]
    key = canonical_offer_semantics(base, items)
    for changed in ({"is_new_customer_required": None}, {"conditions": None},
                    {"conditions": ["New to You at ACE"]}, {"price_model": "from"},
                    {"offer_raw_text": "Botox 10 units Special $99 with a free peel"}):
        assert canonical_offer_semantics({**base, **changed}, items) != key
    assert canonical_offer_semantics(base, [{**items[0], "delivered_units": 20}]) != key
    assert canonical_offer_semantics(base, items + [{"service_id": 2, "item_name": "Forma"}]) != key
