"""Tests for skincare product helpers."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.offer_scope_filter import is_skincare_product_offer, should_exclude_from_offer_master
from utils.retail_paths import is_retail_catalog_url
from utils.skincare_products import build_skincare_product_insert_row, infer_product_name


def test_retail_catalog_url():
    assert is_retail_catalog_url("https://example.com/collections/all")
    assert not is_retail_catalog_url("https://example.com/specials")


def test_skincare_product_offer_detection():
    assert is_skincare_product_offer({"service_name": "Skincare Product", "offer_raw_text": "$15.99"})
    assert is_skincare_product_offer(
        {
            "service_name": "As I Am Cocount Cowash",
            "source_url": "https://example.com/collections/all",
            "offer_raw_text": "As I Am Cocount Cowash $15.99",
        }
    )
    assert not is_skincare_product_offer(
        {"service_name": "Botox", "source_url": "https://example.com/collections/all", "offer_raw_text": "$11/unit"}
    )


def test_build_skincare_product_insert_row():
    row = build_skincare_product_insert_row(
        {
            "source_url": "https://mirrormirror.com/collections",
            "source_name": "mirrormirror.com",
            "service_name": "Skincare Product",
            "offer_raw_text": "Amika Kit $300.00",
            "discount_price": 300,
        }
    )
    assert row["product_name"] == "Amika Kit"
    assert row["discount_price"] == 300
    assert row["domain_name"] == "mirrormirror.com"


def test_infer_product_name_from_raw():
    assert infer_product_name({"service_name": "Skincare Product", "offer_raw_text": "CBD Lip Balm $10.00"}) == "CBD Lip Balm"


def test_should_exclude_skincare():
    assert should_exclude_from_offer_master(
        {"service_name": "Skincare Product", "source_url": "https://x.com/collections", "offer_raw_text": "$9"}
    )
