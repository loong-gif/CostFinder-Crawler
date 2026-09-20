import json
import re
from pathlib import Path

import pytest

from utils.clinic_service_taxonomy import (
    SERVICE_CATEGORY_ENUM_VALUES,
    SERVICE_NAME_ENUM_VALUES,
    live_clinic_service_payload,
    service_name_enum_hint,
)


def test_service_enum_hint_uses_supported_standard_service_names() -> None:
    assert service_name_enum_hint("Botox") == "Botox"
    assert service_name_enum_hint("Juvéderm") == "Juvéderm"
    assert service_name_enum_hint("Juvederm") == "Juvéderm"
    assert service_name_enum_hint("Morpheus8") == "Morpheus8"
    assert service_name_enum_hint("RF Microneedling") == "RF Microneedling"
    assert service_name_enum_hint("SkinPen Microneedling") == "SkinPen Microneedling"
    assert service_name_enum_hint("BBL Photofacial") == "BBL Photofacial"
    assert service_name_enum_hint("NAD Therapy") == "NAD Therapy"
    assert service_name_enum_hint("Pearl Fractional") == "Pearl Fractional"
    assert service_name_enum_hint("Versa") == "Versa"
    assert service_name_enum_hint("Revanesse") == "Versa"
    assert service_name_enum_hint("EZ Gel") == "EZ Gel"
    assert service_name_enum_hint("Ultherapy") == "Ultherapy"


def test_service_enum_hint_maps_common_offer_variants() -> None:
    assert service_name_enum_hint("PRP Microneedling") == "Microneedling"
    assert service_name_enum_hint("Morpheus8 Body") == "Morpheus8"
    assert service_name_enum_hint("NAD+ Injections") == "NAD Therapy"
    assert service_name_enum_hint("Semaglutide") == "Weight Loss"
    assert service_name_enum_hint("Voluma") == "Juvéderm"


def test_service_enum_hint_does_not_guess_unknown_names() -> None:
    assert service_name_enum_hint("Unlisted facial treatment") is None
    assert service_name_enum_hint("Breast Augmentation") is None
    assert service_name_enum_hint("Zo Skin Health") is None


def test_taxonomy_matches_live_database_enum_values() -> None:
    assert len(SERVICE_NAME_ENUM_VALUES) == 84
    assert len(SERVICE_CATEGORY_ENUM_VALUES) == 10
    assert "Sculptra" in SERVICE_NAME_ENUM_VALUES
    assert "Skin Tightening" in SERVICE_CATEGORY_ENUM_VALUES
    assert service_name_enum_hint("Sculptra") == "Sculptra"
    assert service_name_enum_hint("Prf") == "PRF"


def test_live_payload_rejects_service_names_outside_the_database_enum() -> None:
    with pytest.raises(ValueError, match="unsupported clinic service enum"):
        live_clinic_service_payload({"business_id": 1, "service_name": "Breast Augmentation"})


def test_service_extraction_schema_does_not_request_a_canonical_category() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "clinic_services_firecrawl_extraction_schema.json"
    service = json.loads(schema_path.read_text(encoding="utf-8"))["properties"]["services"]["items"]

    assert "service_category" not in service["required"]
    assert "service_category_raw" in service["properties"]


def test_service_extraction_schema_matches_database_service_enum() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "clinic_services_firecrawl_extraction_schema.json"
    service = json.loads(schema_path.read_text(encoding="utf-8"))["properties"]["services"]["items"]

    assert tuple(service["properties"]["service_name"]["enum"]) == SERVICE_NAME_ENUM_VALUES


def test_category_mapping_migration_covers_only_database_enum_values() -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "supabase"
        / "migrations"
        / "20260919041216_rebuild_clinic_service_category_for_name_20260919.sql"
    )
    sql = migration_path.read_text(encoding="utf-8")
    mappings = dict(re.findall(r"when '([^']+)' then '([^']+)'", sql, flags=re.IGNORECASE))

    assert set(mappings) == set(SERVICE_NAME_ENUM_VALUES)
    assert set(mappings.values()) <= set(SERVICE_CATEGORY_ENUM_VALUES)
    assert mappings["Pearl Fractional"] == "Laser & Light"
    assert mappings["Ultherapy"] == "Skin Tightening"
    assert mappings["Skin Tightening"] == "Skin Tightening"


def test_live_schema_snapshot_records_canonical_name_and_category_columns() -> None:
    snapshot_path = Path(__file__).resolve().parents[1] / "schema" / "live_schema_snapshot.json"
    columns = json.loads(snapshot_path.read_text(encoding="utf-8"))["clinic_services"]

    assert "service_name" in columns
    assert "service_category" in columns
    assert "service_name_enum" not in columns
    assert "service_category_enum" not in columns
    assert "service_name_raw" not in columns
    assert "service_area" in columns
