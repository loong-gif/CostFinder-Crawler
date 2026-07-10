"""Tests for utils.offer_fingerprint."""
from utils.offer_fingerprint import (
    compute_offer_fingerprint,
    normalize_service_name,
    normalize_unit_type,
    offer_fingerprint_key,
)


def test_normalize_unit_type_aliases():
    assert normalize_unit_type("units") == "unit"
    assert normalize_unit_type("Unit") == "unit"
    assert normalize_unit_type("") == ""


def test_normalize_service_name_strips_punctuation():
    assert normalize_service_name("Laser Hair Removal!") == "laser hair removal"


def test_fingerprint_stable_for_same_offer():
    fp1 = compute_offer_fingerprint(
        source_url="https://example.com/promo",
        service_name="Botox",
        unit_type="units",
    )
    fp2 = compute_offer_fingerprint(
        source_url="https://example.com/promo/",
        service_name="botox",
        unit_type="unit",
    )
    assert fp1 == fp2


def test_fingerprint_differs_by_service():
    key_a = offer_fingerprint_key(
        source_url="https://a.com/p",
        service_name="Botox",
        unit_type="unit",
    )
    key_b = offer_fingerprint_key(
        source_url="https://a.com/p",
        service_name="Juvederm",
        unit_type="unit",
    )
    assert key_a != key_b
