from utils.promo_offer_item_catalog_backfill import build_catalog_backfill_plan


def _offer(*, item_id: int = 10, text: str = "Botox 40 units for $400") -> dict:
    return {
        "id": 1,
        "business_id": 7,
        "offer_raw_text": text,
        "clinic_promotions": {"source_url": "https://example.com/specials"},
        "promo_offer_items": [{"offer_item_id": item_id, "service_id": None}],
    }


def test_build_catalog_backfill_plan_creates_missing_service_and_links_item() -> None:
    plan = build_catalog_backfill_plan(
        [_offer()],
        [],
        [
            {
                "source_url": "https://example.com/specials",
                "offer_raw_text": "Botox 40 units for $400",
                "service_name": "Botox",
                "service_category": "Neurotoxins",
                "unit_type": "unit",
            }
        ],
    )

    assert plan.services == [
        {
            "business_id": 7,
            "service_name": "Botox",
            "service_category": "Neurotoxin",
            "unit_type": "unit",
            "source_url": "https://example.com/specials",
            "service_name_raw": "Botox",
        }
    ]
    assert plan.links == [{"offer_item_id": 10, "business_id": 7, "service_name": "Botox"}]
    assert plan.skipped == []


def test_plan_does_not_create_when_csv_evidence_conflicts() -> None:
    plan = build_catalog_backfill_plan(
        [_offer()],
        [],
        [
            {"source_url": "https://example.com/specials", "offer_raw_text": "Botox 40 units for $400", "service_name": "Botox"},
            {"source_url": "https://example.com/specials", "offer_raw_text": "Botox 40 units for $400", "service_name": "Dermal Filler"},
        ],
    )

    assert plan.services == []
    assert plan.links == []
    assert plan.skipped == [{"offer_item_id": 10, "reason": "ambiguous_csv_evidence"}]


def test_plan_does_not_create_duplicate_existing_service() -> None:
    plan = build_catalog_backfill_plan(
        [_offer()],
        [{"service_id": 99, "business_id": 7, "service_name": "Botox"}],
        [{"source_url": "https://example.com/specials", "offer_raw_text": "Botox 40 units for $400", "service_name": "Botox"}],
    )

    assert plan.services == []
    assert plan.links == []
    assert plan.skipped == [{"offer_item_id": 10, "reason": "service_already_exists"}]


def test_plan_uses_other_for_unknown_csv_unit() -> None:
    plan = build_catalog_backfill_plan(
        [_offer()],
        [],
        [{"source_url": "https://example.com/specials", "offer_raw_text": "Botox 40 units for $400", "service_name": "Botox", "unit_type": "treatment/unit"}],
    )

    assert plan.services[0]["unit_type"] == "unit"


def test_canonical_service_name_controls_catalog_category() -> None:
    plan = build_catalog_backfill_plan(
        [_offer()],
        [],
        [{"source_url": "https://example.com/specials", "offer_raw_text": "Botox 40 units for $400", "service_name": "Botox", "service_category": "Fillers & Other Injectables"}],
    )

    assert plan.services[0]["service_category"] == "Neurotoxin"
