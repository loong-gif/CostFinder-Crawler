"""Pydantic insert-row validators."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from utils.db_rows import ClinicServiceInsertRow


def test_clinic_service_insert_row_accepts_valid_payload() -> None:
    row = ClinicServiceInsertRow.model_validate(
        {
            "business_id": 1,
            "service_name": "Botox",
            "regular_price": 12.0,
            "unit_type": "unit",
            "source_url": "https://example.com/services",
        }
    )
    assert row.to_api_dict()["service_category"] == "others"


def test_clinic_service_insert_row_rejects_non_positive_price() -> None:
    with pytest.raises(ValidationError):
        ClinicServiceInsertRow.model_validate(
            {
                "business_id": 1,
                "service_name": "Botox",
                "regular_price": 0,
                "unit_type": "unit",
                "source_url": "https://example.com",
            }
        )


def test_clinic_service_insert_row_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ClinicServiceInsertRow.model_validate(
            {
                "business_id": 1,
                "service_name": "Botox",
                "regular_price": 12.0,
                "unit_type": "unit",
                "source_url": "https://example.com",
                "explanation": "should not be here",
            }
        )


def test_upsert_extracted_service_skips_on_schema_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    from utils.clinic_service_extraction import upsert_extracted_service

    class _Client:
        def insert_rows(self, *_args, **_kwargs):
            raise AssertionError("insert_rows must not run when pydantic rejects payload")

    monkeypatch.setattr(
        "utils.clinic_service_extraction.fetch_service_row",
        lambda *_a, **_k: None,
    )
    result = upsert_extracted_service(
        _Client(),
        business_id=1,
        item={"service_name": "Botox", "regular_price": 0, "unit_type": "unit"},
        source_url="https://example.com",
        evidence="",
    )
    assert result["accepted"] is False
    assert result["reason"] == "missing_service_or_price"
    assert result["action"] == "skipped"
