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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("build_initial_offer_load_plan tests passed")