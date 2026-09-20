"""Tests for utils.offer_fingerprint."""
import hashlib

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


def test_fingerprint_uses_parent_and_normalized_raw_text():
    fp1 = compute_offer_fingerprint(
        source_url="https://example.com/promo",
        service_name="Botox",
        offer_raw_text="Botox — $12 / unit!",
        business_id=1,
        promotion_id=2,
    )
    fp2 = compute_offer_fingerprint(
        source_url="https://other.example.com/ignored",
        service_name="Different service",
        offer_raw_text=" botox 12 unit ",
        business_id=999,
        promotion_id=2,
    )
    assert fp1 == fp2
    assert fp1 == hashlib.sha1(b"p:2|botox 12 unit").hexdigest()


def test_fingerprint_distinguishes_parent_source_key():
    key_a = compute_offer_fingerprint(
        source_url="https://a.com/p",
        service_name="Botox",
        offer_raw_text="$12 Botox",
        business_id=1,
        promotion_id=2,
    )
    key_b = compute_offer_fingerprint(
        source_url="https://a.com/p",
        service_name="Botox",
        offer_raw_text="$12 Botox",
        business_id=1,
        promotion_id=3,
    )
    assert key_a != key_b
