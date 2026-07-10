"""Tests for utils.offer_field_normalize."""
from utils.offer_field_normalize import (
    normalize_bool,
    normalize_offer_field_values,
    normalize_service_area,
    prefer_longer_offer_raw_text,
)


def test_normalize_bool_strings():
    assert normalize_bool("true") is True
    assert normalize_bool("FALSE") is False
    assert normalize_bool(True) is True


def test_normalize_service_area_lower():
    assert normalize_service_area("Face") == "face"


def test_prefer_longer_offer_raw_text_from_content():
    offer = {
        "offer_raw_text": "Botox",
        "offer_content": "Botox $11/unit limited time special for new guests",
    }
    text = prefer_longer_offer_raw_text(offer["offer_raw_text"], offer)
    assert len(text) >= 20
    assert "limited time" in text


def test_normalize_offer_field_values_units_and_bool():
    payload = {
        "unit_type": "units",
        "service_area": "Face",
        "is_membership_required": "true",
        "is_package": "false",
    }
    out = normalize_offer_field_values(payload)
    assert out["unit_type"] == "unit"
    assert out["service_area"] == "face"
    assert out["is_membership_required"] is True
    assert out["is_package"] is False
