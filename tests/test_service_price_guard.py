"""Tests for clinic_services catalog price guards and offer item normalization."""
from utils.extraction_persist import build_master_from_offer
from utils.extraction_repair import plan_service_price_repairs
from utils.service_price_guard import (
    derive_offer_item_pricing,
    is_catalog_ineligible_url,
    normalize_service_catalog_item,
    prepare_service_catalog_write,
    should_replace_source_url,
)


def test_rejects_blog_and_promo_urls() -> None:
    assert is_catalog_ineligible_url("https://example.com/blogs/news/botox-specials")
    assert is_catalog_ineligible_url("https://laqueenmedspa.com/promotion")
    assert not is_catalog_ineligible_url("https://quiktox.com/pricing")


def test_rejects_market_average_evidence() -> None:
    decision = normalize_service_catalog_item(
        {"service_name": "Botox", "regular_price": 10, "unit_type": "unit"},
        source_url="https://example.com/services",
        evidence="Botox typically ranges from $10 to $20 per unit in Orange County.",
    )
    assert not decision.accepted
    assert decision.reason == "market_average_not_clinic_price"


def test_rejects_botox_lip_flip_as_unit_catalog() -> None:
    decision = normalize_service_catalog_item(
        {
            "service_name": "Botox",
            "service_name_raw": "Botox Lip Flip",
            "regular_price": 60,
            "unit_type": "treatment",
        },
        source_url="https://alchemyfacebar.com/pages/cosmetic-injectables",
        evidence="Botox Lip Flip | $60",
    )
    assert not decision.accepted
    assert decision.reason == "named_subtreatment_not_unit_catalog"


def test_accepts_combined_neurotoxin_unit_price() -> None:
    decision = normalize_service_catalog_item(
        {"service_name": "Botox", "regular_price": 12, "unit_type": "unit"},
        source_url="https://alchemyfacebar.com/pages/cosmetic-injectables",
        evidence="Botox | Dysport | Xeomin $12 per unit",
    )
    assert decision.accepted
    assert decision.normalized_item is not None
    assert decision.normalized_item["regular_price"] == 12


def test_accepts_per_unit_pricing_page() -> None:
    decision = normalize_service_catalog_item(
        {"service_name": "Botox", "regular_price": 13, "unit_type": "unit"},
        source_url="https://example.com/services/botox",
        evidence="Botox Cosmetic $13/unit for 0-49 units.",
    )
    assert decision.accepted
    assert decision.normalized_item is not None
    assert decision.normalized_item["regular_price"] == 13


def test_normalizes_package_total_to_unit_price() -> None:
    decision = normalize_service_catalog_item(
        {
            "service_name": "Botox",
            "regular_price": 245,
            "unit_type": "area",
            "service_name_raw": "Botox up to 20U $245",
        },
        source_url="https://quiktox.com/pricing",
        evidence="New patient special: Botox up to 20U for $245.",
    )
    assert decision.accepted
    assert decision.normalized_item is not None
    assert decision.normalized_item["regular_price"] == 12.25
    assert decision.normalized_item["unit_type"] == "unit"


def test_rejects_promo_sourced_catalog_write() -> None:
    decision = prepare_service_catalog_write(
        {"service_name": "Xeomin", "regular_price": 450, "unit_type": "session"},
        source_url="https://laqueenmedspa.com/promotion",
        evidence="Xeomin 50 units Regular $450 Special $350",
    )
    assert not decision.accepted
    assert decision.reason == "ineligible_source_url"


def test_should_replace_source_url_prefers_better_page() -> None:
    assert should_replace_source_url(
        "https://example.com/blogs/news/botox",
        "https://example.com/services/botox",
    )
    assert not should_replace_source_url(
        "https://example.com/services/botox",
        "https://example.com/blogs/news/botox",
    )


def test_derive_offer_item_pricing_for_xeomin_package() -> None:
    offer = {
        "regular_price": 450,
        "discount_price": 350,
        "offer_raw_text": "Xeomin 50 units Regular $450 Special $350",
        "items": [{"service_name": "Xeomin", "quantity": None, "unit_price": None}],
    }
    items = derive_offer_item_pricing(offer)
    assert items[0]["quantity"] == 50
    assert items[0]["unit_price"] == 7


def test_build_master_from_offer_carries_derived_item_pricing() -> None:
    offer = {
        "regular_price": 450,
        "discount_price": 350,
        "offer_raw_text": "Xeomin 50 units Regular $450 Special $350",
        "items": [{"service_name": "Xeomin"}],
    }
    master, items = build_master_from_offer(
        offer,
        business_id=456,
        promotion_id=15,
        source_url="https://laqueenmedspa.com/promotion",
    )
    assert master["regular_price"] == 450
    assert master["discount_price"] == 350
    assert items[0]["quantity"] == 50
    assert items[0]["unit_price"] == 7


def test_plan_service_price_repairs_includes_confirmed_rows() -> None:
    services = [
        {"service_id": 28, "regular_price": 245, "unit_type": "area"},
        {"service_id": 33, "regular_price": 10, "unit_type": "unit", "source_url": "https://glowupmedspa.com/blogs/news/x"},
        {"service_id": 34, "regular_price": 450, "unit_type": "session", "source_url": "https://laqueenmedspa.com/promotion"},
    ]
    actions = [a for a in plan_service_price_repairs(services) if a.get("action") == "update"]
    tables = {(a["table"], a["id"], tuple(sorted((a.get("fields") or {}).items()))) for a in actions}
    assert ("clinic_services", 28, (("regular_price", 12.25), ("unit_type", "unit"))) in tables
    assert ("clinic_services", 33, (("regular_price", None), ("source_url", None))) in tables
    assert ("clinic_services", 34, (("regular_price", 9), ("source_url", None), ("unit_type", "unit"))) in tables
    assert ("promo_offer_master", 35, (("discount_price", 350), ("regular_price", 450))) in tables
    assert ("promo_offer_items", 31, (("quantity", 50), ("unit_price", 7))) in tables


def test_mixed_page_batch_skips_ineligible_service_urls() -> None:
    import importlib.util
    from pathlib import Path

    module_path = Path(__file__).resolve().parents[1] / "one-off" / "20260722_irvine_botox_extract.py"
    spec = importlib.util.spec_from_file_location("irvine_botox_extract", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    audit: dict = {
        "llm": {"calls": 0, "failures": []},
        "validated": {"services": []},
        "rejected_extractions": [],
    }
    pages = [
        {
            "id": 1,
            "source_url": "https://laqueenmedspa.com/promotion",
            "markdown": "Xeomin 50 units $450",
        },
        {
            "id": 2,
            "source_url": "https://laqueenmedspa.com/services",
            "markdown": "Services menu",
        },
    ]

    class _LLM:
        def create_json_response(self, *_args, **_kwargs):
            return {"services": [{"service_name": "Xeomin", "regular_price": 450, "unit_type": "session"}]}

    writes = module.extract_services_for_business(
        client=None,
        llm=_LLM(),
        schema={},
        business_id=456,
        pages=pages,
        apply=False,
        audit=audit,
    )
    assert len(writes) == 1
    assert writes[0]["source_url"].endswith("/services")
    assert audit["skipped"]["catalog_ineligible_urls"][0]["source_url"].endswith("/promotion")
