from scripts.build_service_mapping_repair_dryrun import build_report, classify_item


def _item(name: str, *, offer_id: int = 1, offer_item_id: int = 10) -> dict:
    return {
        "offer_id": offer_id,
        "offer_item_id": offer_item_id,
        "business_id": 7,
        "item_name": name,
        "service_id": None,
        "offer_raw_text": f"Special: {name} $100",
        "source_url": "https://example.com/specials",
    }


def test_unique_existing_service_is_link_candidate() -> None:
    result = classify_item(
        _item("SkinPen"),
        [{"service_id": 42, "business_id": 7, "service_name": "SkinPen Microneedling", "service_area": None}],
    )
    assert result["repair_action"] == "LINK_EXISTING_SERVICE"
    assert result["normalized_service_name"] == "SkinPen Microneedling"
    assert result["existing_service_id"] == 42


def test_duplicate_existing_services_require_review() -> None:
    result = classify_item(
        _item("Botox"),
        [
            {"service_id": 1, "business_id": 7, "service_name": "Botox", "service_area": "face"},
            {"service_id": 2, "business_id": 7, "service_name": "Botox", "service_area": "body"},
        ],
    )
    assert result["repair_action"] == "REVIEW"
    assert result["review_reason"] == "multiple_existing_service_candidates"


def test_taxonomy_excluded_service_is_reviewed_separately() -> None:
    result = classify_item(_item("Sculptra"), [])
    assert result["repair_action"] == "TAXONOMY_RESTORE_REVIEW"
    assert result["taxonomy_status"] == "excluded_from_current_enum"


def test_generic_scope_is_not_turned_into_a_service() -> None:
    result = classify_item(_item("All Services"), [])
    assert result["repair_action"] == "EXPAND_GENERIC_SCOPE"
    assert result["normalized_service_name"] is None


def test_package_is_decomposed_when_components_are_known() -> None:
    result = classify_item(_item("BBL + MOXI Package"), [])
    assert result["repair_action"] == "DECOMPOSE_PACKAGE"
    assert result["atomic_service_names"] == ["BBL Photofacial", "Moxi Laser"]


def test_report_includes_active_and_inactive_unresolved_offers() -> None:
    offers = [
        {
            "id": 1,
            "business_id": 7,
            "is_active": True,
            "offer_raw_text": "Botox $100",
            "promotion_id": 9,
            "membership_plan_id": None,
            "promo_offer_items": [{"offer_item_id": 10, "item_name": "Botox", "service_id": None}],
        },
        {
            "id": 2,
            "business_id": 7,
            "is_active": False,
            "offer_raw_text": "Sculptra $100",
            "promotion_id": None,
            "membership_plan_id": 3,
            "promo_offer_items": [{"offer_item_id": 11, "item_name": "Sculptra", "service_id": None}],
        },
    ]
    report = build_report(
        offers,
        [{"service_id": 42, "business_id": 7, "service_name": "Botox", "service_area": None}],
        [{"promotion_id": 9, "business_id": 7, "source_url": "https://example.com/promo", "is_active": True}],
        [{"plan_id": 3, "business_id": 7, "source_url": "https://example.com/membership"}],
    )
    assert report["summary"]["unresolved_offer_count"] == 2
    assert report["summary"]["active_unresolved_item_count"] == 1
    assert report["summary"]["by_repair_action"] == {
        "LINK_EXISTING_SERVICE": 1,
        "TAXONOMY_RESTORE_REVIEW": 1,
    }
