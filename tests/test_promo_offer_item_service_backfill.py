from utils.promo_offer_item_service_backfill import build_backfill_plan, build_catalog_backfill_plan
from utils.clinic_services_db import _skeleton_service_category


def _offer(*, item_id: int = 10, text: str = "Botox 40 units for $400", item_count: int = 1):
    return {
        "id": 1,
        "business_id": 7,
        "offer_raw_text": text,
        "promo_offer_items": [
            {"offer_item_id": item_id + index, "service_id": None, "quantity": 40}
            for index in range(item_count)
        ],
    }


def _service(service_id: int, name: str, business_id: int = 7):
    return {"service_id": service_id, "business_id": business_id, "service_name": name}


def test_csv_evidence_links_one_unlinked_item_to_unique_same_business_service() -> None:
    plan = build_backfill_plan(
        [_offer()],
        [_service(99, "Botox"), _service(100, "Botox", business_id=8)],
        [{"offer_raw_text": "Botox 40 units for $400", "service_name": "Botox"}],
    )

    assert plan.links == [{"offer_item_id": 10, "service_id": 99, "reason": "csv_exact_text"}]
    assert plan.skipped == []


def test_conflicting_csv_names_for_same_text_are_not_linked() -> None:
    plan = build_backfill_plan(
        [_offer()],
        [_service(99, "Botox"), _service(101, "Dermal Filler")],
        [
            {"offer_raw_text": "Botox 40 units for $400", "service_name": "Botox"},
            {"offer_raw_text": "Botox 40 units for $400", "service_name": "Dermal Filler"},
        ],
    )

    assert plan.links == []
    assert plan.skipped[0]["reason"] == "ambiguous_csv_evidence"


def test_source_url_disambiguates_identical_csv_offer_text() -> None:
    offer = _offer(text="Summer special")
    offer["clinic_promotions"] = {"source_url": "https://example.com/botox"}
    plan = build_backfill_plan(
        [offer],
        [_service(99, "Botox"), _service(101, "Dermal Filler")],
        [
            {"source_url": "https://example.com/botox", "offer_raw_text": "Summer special", "service_name": "Botox"},
            {"source_url": "https://other.example/filler", "offer_raw_text": "Summer special", "service_name": "Dermal Filler"},
        ],
    )

    assert plan.links == [{"offer_item_id": 10, "service_id": 99, "reason": "csv_exact_text_and_url"}]


def test_multiple_matching_service_rows_are_not_linked() -> None:
    plan = build_backfill_plan(
        [_offer()],
        [_service(99, "Botox"), _service(101, "Botox")],
        [{"offer_raw_text": "Botox 40 units for $400", "service_name": "Botox"}],
    )

    assert plan.links == []
    assert plan.skipped[0]["reason"] == "ambiguous_service_candidate"


def test_existing_service_id_is_never_overwritten() -> None:
    offer = _offer()
    offer["promo_offer_items"][0]["service_id"] = 77
    plan = build_backfill_plan(
        [offer],
        [_service(99, "Botox")],
        [{"offer_raw_text": "Botox 40 units for $400", "service_name": "Botox"}],
    )

    assert plan.links == []
    assert plan.skipped == []


def test_heuristic_does_not_guess_multi_item_offer() -> None:
    plan = build_backfill_plan(
        [_offer(text="Botox and filler package $700", item_count=2)],
        [_service(99, "Botox")],
        [],
    )

    assert plan.links == []
    assert {row["reason"] for row in plan.skipped} == {"multi_item_without_csv_evidence"}


def test_invalid_records_are_reported_not_silently_ignored() -> None:
    offer = _offer()
    offer["business_id"] = None
    offer["promo_offer_items"][0]["offer_item_id"] = None
    plan = build_backfill_plan([offer], [], [])

    assert plan.links == []
    assert plan.skipped == [{"offer_item_id": None, "reason": "invalid_business_id"}]


def test_catalog_plan_creates_a_missing_canonical_service_then_links_item() -> None:
    plan = build_catalog_backfill_plan([_offer()], [])

    assert plan.creates == [{"business_id": 7, "service_name": "Botox"}]
    assert plan.links == [
        {
            "offer_item_id": 10,
            "business_id": 7,
            "service_name": "Botox",
            "reason": "single_item_inference_created_service",
        }
    ]
    assert plan.skipped == []


def test_catalog_plan_uses_existing_service_without_creating_duplicate() -> None:
    plan = build_catalog_backfill_plan([_offer()], [_service(99, "Botox")])

    assert plan.creates == []
    assert plan.links == [
        {
            "offer_item_id": 10,
            "business_id": 7,
            "service_name": "Botox",
            "service_id": 99,
            "reason": "single_item_inference",
        }
    ]


def test_catalog_plan_does_not_create_for_ambiguous_multi_item_offer() -> None:
    plan = build_catalog_backfill_plan(
        [_offer(text="Botox and filler package $700", item_count=2)], []
    )

    assert plan.creates == []
    assert {row["reason"] for row in plan.skipped} == {"multi_item_without_csv_evidence"}


def test_skeleton_service_category_satisfies_live_not_null_enum() -> None:
    assert _skeleton_service_category("Botox") == "Neurotoxin"
    assert _skeleton_service_category("Juvéderm") == "Filler"
    assert _skeleton_service_category("Microneedling") == "others"
