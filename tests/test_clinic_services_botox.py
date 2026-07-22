"""Self-check for Botox unit-price extraction from crawl markdown."""
from decimal import Decimal

from utils.clinic_services_botox import (
    extract_botox_fields_from_pages,
    extract_botox_fields_from_text,
    website_to_crawl_url,
)


def test_extract_unit_price_near_botox():
    text = "Our menu: Botox $11/unit. Filler starts at $650/syringe."
    fields = extract_botox_fields_from_text(text)
    assert fields.regular_price == Decimal("11")
    assert fields.unit_type == "unit"


def test_extract_prefers_per_unit_over_package_total():
    text = (
        "Botox package 40 units for $399. "
        "Or buy Botox at $10 per unit without a package."
    )
    fields = extract_botox_fields_from_text(text)
    assert fields.regular_price == Decimal("10")
    assert fields.unit_type == "unit"


def test_extract_service_area_hint():
    text = "Botox for crow's feet — $12/unit"
    fields = extract_botox_fields_from_text(text)
    assert fields.regular_price == Decimal("12")
    assert fields.service_area == "crow's feet"


def test_no_botox_returns_empty():
    fields = extract_botox_fields_from_text("Dermal filler $650/syringe")
    assert fields.regular_price is None
    assert fields.unit_type is None


def test_extract_from_pages_picks_first_priced_page():
    pages = [
        {"markdown": "About us — we love Botox"},
        {"markdown": "Pricing: Botox $9.5/unit"},
    ]
    fields = extract_botox_fields_from_pages(pages)
    assert fields.regular_price == Decimal("9.5")
    assert fields.unit_type == "unit"


def test_website_to_crawl_url():
    assert website_to_crawl_url("example.com") == "https://example.com"
    assert website_to_crawl_url("https://a.com/path") == "https://a.com/path"
    assert website_to_crawl_url("") == ""
