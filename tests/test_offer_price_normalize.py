"""Tests for utils.offer_price_normalize."""
from utils.offer_price_normalize import normalize_offer_prices, parse_price


def test_parse_price_strips_currency():
    assert parse_price("$1,299.50") == 1299.5


def test_single_price_goes_to_regular_only():
    result = normalize_offer_prices(offer_raw_text="Laser Peel $299")
    assert result["regular_price"] == 299.0
    assert result["discount_price"] is None


def test_was_now_extracts_pair():
    result = normalize_offer_prices(offer_raw_text="Was $500 now $399")
    assert result["regular_price"] == 500.0
    assert result["discount_price"] == 399.0
    assert result["discount_amount"] == 101.0


def test_swaps_when_discount_gt_regular():
    result = normalize_offer_prices(regular_price=100, discount_price=150)
    assert result["regular_price"] == 150.0
    assert result["discount_price"] == 100.0

    result = normalize_offer_prices(regular_price=150, discount_price=100)
    assert result["regular_price"] == 150.0
    assert result["discount_price"] == 100.0


def test_original_price_maps_to_regular():
    result = normalize_offer_prices(original_price="200", discount_price="150")
    assert result["regular_price"] == 200.0
    assert result["discount_price"] == 150.0
    assert result["discount_percent"] == 25.0
