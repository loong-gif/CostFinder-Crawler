from copy import deepcopy

from utils.offer_evidence_validator import validate_offer_evidence
from utils.offer_purchase_semantics import (
    canonical_offer_semantics, normalize_purchase_offers, service_supported_by_source, usable_unit_price,
)


def _offer(raw, **changes):
    return {"offer_raw_text": raw, "regular_price": None, "discount_price": 99,
            "discount_percent": None, "discount_amount": None, "conditions": [],
            "is_membership_required": False, "is_new_customer_required": False,
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


def test_controlled_alias_is_evidence_but_unrelated_catalogue_mapping_is_not():
    assert service_supported_by_source("Dermal Filler", "Lip Filler special $199")
    assert not service_supported_by_source("Botox", "20% off favorite treatments")


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
