from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

SCRIPT = PROJECT_ROOT / "scripts" / "build_initial_offer_load_plan.py"
spec = importlib.util.spec_from_file_location("build_initial_offer_load_plan", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

from utils.offer_extraction_llm import normalize_offer_payload


class FakeLlmClient:
    pass


def fake_extractor(row, client):
    return {
        "selected_segments": [{"index": 6, "reason": "price row"}],
        "candidate_block_selection": {"summary": "ok"},
        "offers": [
            {
                "service_name": "Botox",
                "service_category": "Injectables",
                "offer_raw_text": "Injectables Botox $11 Per Unit",
                "original_price": "11",
                "unit_type": "unit",
                "evidence_segments": [6],
            }
        ],
    }


def test_build_page_plan_without_llm_emits_segments_and_empty_plan():
    row = {
        "promo_website_id": 1,
        "domain_name": "revivemedspaokc.com",
        "subpage_url": "https://revivemedspaokc.com/pricing",
        "page_content": "[SEGMENT 6]Injectables Botox $11 Per Unit",
    }

    page = module.build_page_plan(row, None)

    assert page["segment_summary"]["offer_signal_count"] == 1
    assert page["offers"] == []
    assert page["plan"]["summary"]["master_rows"] == 0


def test_build_page_plan_with_llm_builds_master_and_evidence_rows():
    row = {
        "promo_website_id": 1,
        "business_id": 2,
        "domain_name": "revivemedspaokc.com",
        "subpage_url": "https://revivemedspaokc.com/pricing",
        "page_content": "[SEGMENT 6]Injectables Botox $11 Per Unit",
    }

    page = module.build_page_plan(row, FakeLlmClient(), extractor=fake_extractor)

    assert len(page["offers"]) == 1
    assert page["plan"]["summary"]["master_rows"] == 1
    assert page["plan"]["summary"]["evidence_rows"] == 1
    master = page["plan"]["master_rows"][0]
    assert master["canonical_service_name"] == "Botox"
    assert master["price_model"] == "per_unit"
    assert page["plan"]["evidence_rows"][0]["offer_fingerprint"] == master["offer_fingerprint"]


def test_build_page_plan_can_use_chunking_extractor_without_losing_segments():
    row = {
        "promo_website_id": 1,
        "business_id": 2,
        "domain_name": "revivemedspaokc.com",
        "subpage_url": "https://revivemedspaokc.com/pricing",
        "page_content": "[SEGMENT 6]Botox $11 Per Unit\n[SEGMENT 7]Dysport $3.70 Per Unit",
    }

    def extractor(row, client):
        return {
            "selected_segments": [{"index": 6}, {"index": 7}],
            "candidate_block_selection": {"summary": "all selected"},
            "offer_extraction_chunks": 2,
            "offers": [
                {
                    "service_name": "Botox",
                    "display_service_name": "Botox",
                    "canonical_service_name": "Botox",
                    "offer_raw_text": "Botox $11 Per Unit",
                    "original_price": "11",
                    "unit_type": "unit",
                    "evidence_segments": [6],
                },
                {
                    "service_name": "Dysport",
                    "display_service_name": "Dysport",
                    "canonical_service_name": "Dysport",
                    "offer_raw_text": "Dysport $3.70 Per Unit",
                    "original_price": "3.70",
                    "unit_type": "unit",
                    "evidence_segments": [7],
                },
            ],
        }

    page = module.build_page_plan(row, FakeLlmClient(), extractor=extractor)

    assert len(page["offers"]) == 2
    assert page["plan"]["summary"]["master_rows"] == 2
    assert {item["canonical_service_name"] for item in page["plan"]["master_rows"]} == {"Botox", "Dysport"}


def test_offer_extraction_normalizes_service_enum_and_preserves_display():
    payload = {
        "offers": [
            {
                "service_name": "Restylane Kysse",
                "display_service_name": "Restylane Kysse",
                "canonical_service_name": "Restylane Kysse",
                "offer_raw_text": "Restylane Kysse $650/syringe",
                "evidence_segments": [10],
            },
            {
                "service_name": "Botox",
                "display_service_name": "Botox",
                "canonical_service_name": "Botox",
                "offer_raw_text": "Botox $11 Per Unit",
                "evidence_segments": [11],
            },
            {
                "service_name": "Mystery Glow",
                "display_service_name": "Mystery Glow",
                "offer_raw_text": "Mystery Glow $199",
                "evidence_segments": [12],
            },
        ]
    }

    offers = normalize_offer_payload(payload, allowed_indexes={10, 11, 12})["offers"]

    assert offers[0]["display_service_name"] == "Restylane Kysse"
    assert offers[0]["service_name"] == "Dermal Filler"
    assert offers[0]["canonical_service_name"] == "Dermal Filler"
    assert offers[1]["display_service_name"] == "Botox"
    assert offers[1]["service_name"] == "Botox"
    assert offers[1]["canonical_service_name"] == "Botox"
    assert offers[2]["display_service_name"] == "Mystery Glow"
    assert offers[2]["service_name"] == "Others"
    assert offers[2]["canonical_service_name"] == "Others"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("build_initial_offer_load_plan tests passed")
