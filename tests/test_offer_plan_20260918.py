from utils.offer_plan_20260918 import (
    extract_explicit_terms,
    should_clear_per_unit_quantity,
    split_offer_fingerprint,
)


def test_extracts_minimum_code_and_not_combinable_terms():
    raw = (
        "New patients receive $100 off when they purchase 40 or more units. "
        "Use code 15OFFFILLER. Not combinable with any other offer."
    )

    assert extract_explicit_terms(raw) == [
        "minimum 40 units",
        "promo code 15OFFFILLER",
        "Cannot be combined with other offers",
    ]


def test_per_unit_quantity_one_is_not_a_delivered_quantity():
    assert should_clear_per_unit_quantity("$10/unit Botox") is True
    assert should_clear_per_unit_quantity("extra Botox units $10 versus $13") is True
    assert should_clear_per_unit_quantity("$9 per Botox unit beyond the first 20 units") is True
    assert should_clear_per_unit_quantity("$1 off each unit of Xeomin") is True
    assert should_clear_per_unit_quantity("30 units of Botox for $300") is False


def test_split_fingerprint_includes_atomic_service_identity():
    common = {
        "source_key": "p:215",
        "offer_raw_text": "15% off any Juvéderm or Restylane filler treatment",
        "discount_percent": 15,
    }

    assert split_offer_fingerprint(service_name="Juvéderm", **common) != split_offer_fingerprint(
        service_name="Restylane", **common
    )
