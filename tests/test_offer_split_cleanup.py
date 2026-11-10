from __future__ import annotations

import pytest

from utils.offer_purchase_semantics import _join_branch, normalize_purchase_offers
from utils.offer_scope_filter import clean_item_name


def offer(raw, names, **fields):
    base = {"purchase_type": "single", "is_package": False, "price_model": "total", "regular_price": None,
            "discount_price": None, "discount_percent": 10, "discount_amount": None, "min_quantity": None,
            "is_membership_required": False, "is_new_customer_required": False, "conditions": [],
            "offer_raw_text": raw,
            "items": [{"item_name": n, "service_name": None, "quantity": None, "unit_price": None,
                       "unit_type": None, "service_area": None} for n in names]}
    return {**base, **fields}


def test_join_branch_skips_punctuation_only_parts():
    assert _join_branch("15% off", "retail", ".") == "15% off | retail"
    assert _join_branch("", "retail", " ; ") == "retail"


def test_split_choices_do_not_keep_the_sentence_period_as_a_segment():
    raw = "10% off retail and injectables."
    offers, _ = normalize_purchase_offers([offer(raw, ["retail", "injectables"])], raw)
    assert [o["offer_raw_text"] for o in offers] == ["10% off | retail", "10% off | injectables"]


@pytest.mark.parametrize("name,expected", [
    ("neuromodulators at standard prices", "neuromodulators"),
    ("one full syringe of dermal filler", "dermal filler"),
    ("2 vials of Sculptra", "Sculptra"),
    ("Botox", "Botox"),
    ("3 Areas Botox", "3 Areas Botox"),
    ("Smile Line Filler (Single Syringe)", "Smile Line Filler (Single Syringe)"),
])
def test_clean_item_name_removes_price_phrase_and_quantity_prefix_only(name, expected):
    assert clean_item_name(name) == expected
