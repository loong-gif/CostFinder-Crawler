from utils.promo_offer_scope_expansion import build_expansion_plan


def _offer(offer_id: int, business_id: int, text: str, **fields: object) -> dict:
    return {
        "id": offer_id, "business_id": business_id, "promotion_id": 1,
        "offer_raw_text": text, "discount_percent": 20, "price_model": "total",
        "is_active": True, "clinic_promotions": {"source_url": "https://example.com/specials"},
        **fields,
    }


def _service(service_id: int, business_id: int, name: str, price=None, category="") -> dict:
    return {"service_id": service_id, "business_id": business_id, "service_name": name,
            "regular_price": price, "unit_type": "unit", "service_category": category}


def test_global_offer_expands_to_every_same_business_service_and_reprices() -> None:
    plan = build_expansion_plan([_offer(1338, 1019, "Enjoy 20% OFF On Your First Visit With Us.")], [
        _service(1, 1019, "Botox", 15), _service(2, 1019, "Dysport", None), _service(3, 88, "Xeomin", 10),
    ])
    assert [row["service_name"] for row in plan.creates] == ["Botox", "Dysport"]
    assert plan.creates[0]["master"]["regular_price"] == 15
    assert plan.creates[0]["master"]["discount_price"] == 12
    assert plan.creates[1]["master"]["regular_price"] is None
    assert plan.creates[1]["master"]["discount_price"] is None
    assert plan.deactivate_offer_ids == [1338]


def test_category_offer_expands_only_category_services() -> None:
    plan = build_expansion_plan([_offer(2, 7, "26% off all neurotoxin treatments")], [
        _service(1, 7, "Botox", 10, "Neurotoxin"), _service(2, 7, "Dermal Filler", 500, "Filler"),
        _service(3, 7, "Xeomin", 9, "Neurotoxin"),
    ])
    assert [row["service_name"] for row in plan.creates] == ["Botox", "Xeomin"]


def test_multiple_named_services_creates_missing_catalog_service() -> None:
    plan = build_expansion_plan([_offer(1295, 241, "Botox/Jeuveau - Staff $9.50 per unit with 40 unit minimum Save $2.50 per unit off regular price", regular_price=12, discount_price=9.5, discount_percent=None, discount_amount=2.5, price_model="per_unit")], [_service(11, 241, "Botox", 12)])
    assert plan.service_creates == [{"business_id": 241, "service_name": "Jeuveau", "service_name_raw": "Jeuveau", "service_category": "Neurotoxin", "unit_type": "unit", "source_url": "https://example.com/specials", "regular_price": 12.0}]
    assert [row["service_name"] for row in plan.creates] == ["Botox", "Jeuveau"]
    assert all(row["item"]["quantity"] == 40 and row["item"]["unit_price"] == 9.5 for row in plan.creates)
    assert all(row["master"]["price_model"] == "total" and row["master"]["regular_price"] == 480 and row["master"]["discount_price"] == 380 for row in plan.creates)


def test_package_and_area_text_are_not_split() -> None:
    plan = build_expansion_plan([_offer(4, 7, "Package includes Botox and Dysport for forehead and jawline")], [_service(1, 7, "Botox"), _service(2, 7, "Dysport")])
    assert plan.creates == []
    assert plan.deactivate_offer_ids == []
    assert plan.skipped[0]["reason"] == "bundle_or_area"


def test_vial_combo_and_existing_single_service_offer_are_not_reexpanded() -> None:
    combo = _offer(41, 7, "Luxe Lift: 3 vials of filler + 20u Botox or 60u Dysport")
    derived = _offer(42, 7, "Botox or Dysport $10 per unit", promo_offer_items=[{"service_id": 1}])
    plan = build_expansion_plan([combo, derived], [_service(1, 7, "Botox"), _service(2, 7, "Dysport")])
    assert plan.creates == []
    assert plan.skipped == [{"offer_id": 41, "reason": "bundle_or_area"}]


def test_named_service_mentions_are_deduplicated() -> None:
    plan = build_expansion_plan([_offer(5, 7, "Botox or Dysport; Botox is $10 per unit")], [_service(1, 7, "Botox", 12), _service(2, 7, "Dysport", 4)])
    assert [row["service_name"] for row in plan.creates] == ["Botox", "Dysport"]


def test_missing_catalog_price_keeps_explicit_source_sale_price() -> None:
    plan = build_expansion_plan([_offer(6, 7, "Botox or Dysport $10 per unit", discount_percent=None, discount_price=10)], [_service(1, 7, "Botox", None), _service(2, 7, "Dysport", None)])
    assert all(row["master"]["regular_price"] is None and row["master"]["discount_price"] == 10 for row in plan.creates)


def test_fixed_amount_unit_minimum_uses_total_price_without_negative_discount() -> None:
    plan = build_expansion_plan([_offer(7, 9, "Save $100 when you purchase 40 or more units of Botox or Dysport", discount_percent=None, discount_amount=100)], [_service(1, 9, "Botox", 13), _service(2, 9, "Dysport", 13)])
    assert all(row["master"]["regular_price"] == 520 and row["master"]["discount_price"] == 420 for row in plan.creates)
    assert all(row["item"]["quantity"] == 40 and row["item"]["unit_price"] == 10.5 for row in plan.creates)


def test_non_comparable_catalog_price_is_not_used_as_regular_price() -> None:
    plan = build_expansion_plan([_offer(8, 10, "Botox or Dysport $13.98 per unit", discount_percent=None, discount_price=13.98)], [_service(1, 10, "Botox", 4.66), _service(2, 10, "Dysport", 4.66)])
    assert all(row["master"]["regular_price"] is None and row["master"]["discount_price"] == 13.98 for row in plan.creates)
