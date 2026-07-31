from __future__ import annotations

from pathlib import Path

from utils.sag_structured_corpus import build_structured_documents, materialize_documents


def test_builds_traceable_documents_for_all_three_current_tables() -> None:
    documents = build_structured_documents(
        services=[
            {
                "service_id": 11,
                "business_id": 7,
                "service_name": "Botox",
                "regular_price": 12,
                "unit_type": "unit",
                "service_category": "Neurotoxin",
                "service_name_raw": "Botox cosmetic",
                "service_area": "forehead",
                "source_url": "https://clinic.example/pricing",
                "updated_at": "2026-07-27T00:00:00Z",
            }
        ],
        memberships=[
            {
                "plan_id": 12,
                "business_id": 7,
                "membership_name": "Glow Club",
                "membership_price": 99,
                "billing_period": "monthly",
                "minimum_commitment_months": 3,
                "benefits": [{"type": "discount", "value": 10}],
                "source_url": "https://clinic.example/membership",
                "updated_at": "2026-07-27T00:00:00Z",
            }
        ],
        promotions=[
            {
                "promotion_id": 13,
                "business_id": 7,
                "promotion_title": "Summer Special",
                "campaign_start_date": "2026-07-01",
                "campaign_end_date": "2026-07-31",
                "is_active": True,
                "promotion_content": ["Botox and filler discount"],
                "source_url": "https://clinic.example/specials",
                "updated_at": "2026-07-27T00:00:00Z",
            }
        ],
    )

    assert {(document.record_type, document.record_id) for document in documents} == {
        ("clinic_service", "11"),
        ("clinic_membership", "12"),
        ("clinic_promotion", "13"),
    }
    assert all("Business ID: 7" in document.content for document in documents)
    assert "Source URL: https://clinic.example/pricing" in documents[0].content
    assert "Botox" in documents[0].content
    assert "Glow Club" in documents[1].content
    assert "Summer Special" in documents[2].content


def test_materialization_uses_stable_filenames_without_spaces(tmp_path: Path) -> None:
    document = build_structured_documents(
        services=[
            {
                "service_id": 11,
                "business_id": 7,
                "service_name": "Botox",
                "source_url": "",
                "updated_at": "",
            }
        ],
        memberships=[],
        promotions=[],
    )[0]

    materialized = materialize_documents([document], tmp_path)

    assert materialized[0].path.name.startswith("clinic_service_11_")
    assert materialized[0].path.suffix == ".md"
    assert " " not in materialized[0].path.name
    assert "Source URL: unavailable" in materialized[0].path.read_text(encoding="utf-8")
