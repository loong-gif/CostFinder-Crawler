from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.firecrawl_monitor import EXTRACTION_SCHEMA


def test_schema_has_single_offers_array():
    props = EXTRACTION_SCHEMA["properties"]
    assert "offers" in props
    assert props["offers"]["type"] == "array"
    assert "services" not in props
    assert "memberships" not in props


def test_offer_item_fields_align_master():
    item_props = EXTRACTION_SCHEMA["properties"]["offers"]["items"]["properties"]
    assert item_props["regular_price"]["type"] == "number"
    assert item_props["discount_price"]["type"] == "number"
    for field in ("unit_type", "billing_period", "service_category", "template_type"):
        assert field in item_props
        assert item_props[field]["type"] == "string"


def main() -> None:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("schema checks passed.")


if __name__ == "__main__":
    main()
