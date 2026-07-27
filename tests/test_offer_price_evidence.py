"""Tests for conservative price evidence checks from offer_raw_text."""

from utils.offer_price_evidence import extract_price_evidence, plan_inconsistent_price_field_clears


def test_pair_evidence_clears_only_mismatched_fields():
    fields = plan_inconsistent_price_field_clears(
        {
            "offer_raw_text": "Was $500, now $400 (save $100 / 20% off)",
            "regular_price": 500,
            "discount_price": 0.01,
            "discount_amount": 100,
            "discount_percent": 20,
        }
    )

    assert fields == {"discount_price": None}


def test_pair_evidence_does_not_clear_matching_derived_values():
    fields = plan_inconsistent_price_field_clears(
        {
            "offer_raw_text": "Regular $500; sale $400",
            "regular_price": 500,
            "discount_price": 400,
            "discount_amount": 100,
            "discount_percent": 20,
        }
    )

    assert fields == {}


def test_explicit_amount_evidence_clears_wrong_amount_when_prices_prove_relation():
    fields = plan_inconsistent_price_field_clears(
        {
            "offer_raw_text": "Save $100 on this treatment",
            "regular_price": 500,
            "discount_price": 400,
            "discount_amount": 90,
            "discount_percent": None,
        }
    )

    assert fields == {"discount_amount": None}


def test_explicit_percent_evidence_clears_wrong_percent_when_prices_prove_relation():
    fields = plan_inconsistent_price_field_clears(
        {
            "offer_raw_text": "25% off all laser treatments",
            "regular_price": 400,
            "discount_price": 300,
            "discount_amount": None,
            "discount_percent": 20,
        }
    )

    assert fields == {"discount_percent": None}


def test_unlabelled_single_price_is_not_treated_as_discount_evidence():
    fields = plan_inconsistent_price_field_clears(
        {
            "offer_raw_text": "Botox $12 per unit",
            "regular_price": 12,
            "discount_price": 0.01,
            "discount_amount": None,
            "discount_percent": None,
        }
    )

    assert fields == {}


def test_labelled_sale_price_clears_wrong_discount_price():
    fields = plan_inconsistent_price_field_clears(
        {
            "offer_raw_text": "Sale price: $12 per unit",
            "regular_price": None,
            "discount_price": 0.01,
            "discount_amount": None,
            "discount_percent": None,
        }
    )

    assert fields == {"discount_price": None}


def test_explicit_amount_evidence_clears_discount_price_when_equation_is_impossible():
    fields = plan_inconsistent_price_field_clears(
        {
            "offer_raw_text": "Filler Special $100 off all fillers",
            "regular_price": 101,
            "discount_price": 0.01,
            "discount_amount": 100,
            "discount_percent": None,
        }
    )

    assert fields == {"discount_price": None}


def test_comma_separated_sale_price_is_parsed_as_one_number():
    fields = plan_inconsistent_price_field_clears(
        {
            "offer_raw_text": "Face Rejuvenation for ONLY $1,399 (2 vials)",
            "regular_price": 699.5,
            "discount_price": 699.5,
            "discount_amount": None,
            "discount_percent": None,
        }
    )

    assert fields == {"discount_price": None}


def test_price_pair_with_normal_punctuation_keeps_matching_values():
    fields = plan_inconsistent_price_field_clears(
        {
            "offer_raw_text": "Regular $500, sale $400. Save $100 on Botox.",
            "regular_price": 500,
            "discount_price": 400,
            "discount_amount": 100,
            "discount_percent": 20,
        }
    )

    assert fields == {}


def test_per_unit_pair_with_member_price_is_explicit_price_evidence():
    evidence = extract_price_evidence("Botox $11/unit (regularly $13.50/unit) for members")

    assert evidence == {
        "regular_price": 13.5,
        "discount_price": 11.0,
        "discount_amount": 2.5,
        "discount_percent": 18.52,
    }
