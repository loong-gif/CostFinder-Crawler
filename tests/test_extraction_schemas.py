"""Contract checks for LLM extraction schemas."""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"

EXTRACTION_SCHEMA_FILES = (
    "promotion_extraction_schema.json",
    "offer_extraction_schema.json",
    "membership_extraction_schema.json",
    "clinic_services_firecrawl_extraction_schema.json",
)


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def test_extraction_schemas_have_top_level_explanation_first() -> None:
    for name in EXTRACTION_SCHEMA_FILES:
        schema = _load_schema(name)
        prop_keys = list(schema["properties"].keys())
        assert prop_keys[0] == "explanation", f"{name}: first property must be explanation"
        assert schema["properties"]["explanation"]["type"] == "string"
        assert schema["required"][0] == "explanation", f"{name}: explanation must be first required field"
        assert "explanation" in schema["required"]
        assert schema.get("additionalProperties") is False


def test_offer_schema_embeds_service_items() -> None:
    offer_schema = _load_schema("offer_extraction_schema.json")
    offer = offer_schema["properties"]["offers"]["items"]
    item = offer["properties"]["items"]["items"]

    assert offer["properties"]["items"]["minItems"] == 1
    assert "items" in offer["required"]
    assert item["required"] == [
        "service_name",
        "quantity",
        "unit_price",
        "unit_type",
        "service_area",
    ]
    conditions = offer["properties"]["conditions"]
    assert "conditions" in offer["required"]
    assert conditions["type"] == "array"
    assert conditions["items"]["required"] == ["requirement", "evidence"]
    assert conditions["items"]["additionalProperties"] is False
    assert set(conditions["items"]["properties"]) == {"requirement", "evidence"}
    assert "默认填 1" in item["properties"]["quantity"]["description"]
    service_schema = _load_schema("clinic_services_firecrawl_extraction_schema.json")
    service = service_schema["properties"]["services"]["items"]
    assert "service_name" in service["required"]
    assert service["properties"]["service_name"]["minLength"] == 1
    assert not (SCHEMA_DIR / "offer_items_extraction_schema.json").exists()
