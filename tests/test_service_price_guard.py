"""Tests for clinic_services catalog price guards and offer item normalization."""
from utils.extraction_persist import build_master_from_offer
from utils.extraction_repair import plan_offer_quality_repairs, plan_service_price_repairs
from utils.service_price_guard import (
    derive_offer_item_pricing,
    infer_explicit_per_unit_price,
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
        "conditions": [],
        "offer_raw_text": "Xeomin 50 units Regular $450 Special $350",
        "items": [{"service_name": "Xeomin", "quantity": None, "unit_price": None}],
    }
    items = derive_offer_item_pricing(offer)
    assert items[0]["quantity"] == 50
    assert items[0]["unit_price"] == 7


def test_preserves_explicit_per_unit_offer_price() -> None:
    items = derive_offer_item_pricing(
        {
            "discount_price": 11,
            "items": [{"service_name": "Botox"}],
        },
        evidence="Botox $11 per unit, 20 units minimum",
    )

    assert items == [{"service_name": "Botox", "quantity": 20, "unit_price": 11}]


def test_infers_single_source_stated_per_unit_price() -> None:
    assert infer_explicit_per_unit_price("Botox $11 a unit") == 11
    assert infer_explicit_per_unit_price("Botox $11/unit (regularly $13/unit)") == 11
    assert infer_explicit_per_unit_price("Reg. $3.75/unit; $50 off") is None


def test_membership_rate_uses_first_rate_when_nonmember_rate_is_also_stated() -> None:
    items = derive_offer_item_pricing(
        {
            "is_membership_required": True,
            "offer_raw_text": "$11/u Dysport or Botox ($13/u for non-members)",
            "items": [{"service_name": "Dysport"}, {"service_name": "Botox"}],
        }
    )
    assert [item["unit_price"] for item in items] == [11, 11]


def test_build_master_from_offer_carries_derived_item_pricing() -> None:
    offer = {
        "regular_price": 450,
        "discount_price": 350,
        "conditions": [],
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


def test_build_master_from_offer_promotes_common_item_rate_to_master_price() -> None:
    master, items = build_master_from_offer(
        {
            "conditions": [],
            "discount_amount": 2,
            "offer_raw_text": "$11/u Dysport or Botox ($13/u for non-members)",
            "is_membership_required": True,
            "items": [{"service_name": "Dysport"}, {"service_name": "Botox"}],
        },
        business_id=456,
        promotion_id=15,
        source_url="https://example.com/specials",
    )
    assert master["price_model"] == "per_unit"
    assert master["discount_price"] == 11
    assert [item["unit_price"] for item in items] == [11, 11]


def test_build_master_does_not_broadcast_one_quantity_across_package_items() -> None:
    offer = {
        "discount_price": 750,
        "conditions": [],
        "offer_raw_text": "2 syringes Skinvive + 25 units Botox = $750",
        "items": [
            {"service_name": "Juvéderm"},
            {"service_name": "Botox"},
        ],
    }

    _master, items = build_master_from_offer(
        offer,
        business_id=456,
        promotion_id=15,
        source_url="https://example.com/specials",
    )

    assert all(item.get("quantity") is None for item in items)
    assert all(item.get("unit_price") is None for item in items)


def test_legacy_fingerprint_repair_uses_offer_parent_ids() -> None:
    actions = plan_offer_quality_repairs(
        [
            {
                "id": 10,
                "business_id": 20,
                "promotion_id": 30,
                "membership_plan_id": None,
                "is_active": True,
                "offer_fingerprint": "a" * 32,
                "offer_raw_text": "Botox $9/unit",
                "regular_price": 12,
                "discount_price": 9,
            }
        ],
        promotions=[{"promotion_id": 30, "source_url": "https://example.com/specials"}],
    )

    fingerprint_action = next(action for action in actions if action["batch"] == "offer_fingerprint")
    assert fingerprint_action["fields"]["offer_fingerprint"] == "4b02934ca3d9ccd80bc49c2ebd0d3f6f9cf86eef"


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
