from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_evidence_segments import build_segment_records
from utils.offer_initial_load import (
    build_master_offer_row,
    build_price_signature,
    infer_canonical_service_name,
    infer_offer_type,
    infer_price_model,
    plan_initial_offer_load,
)


def _page(text: str):
    return {
        "promo_website_id": 1863,
        "business_id": 42,
        "domain_name": "revivemedspaokc.com",
        "subpage_url": "https://www.revivemedspaokc.com/pricing/?utm_source=x",
        "page_content": text,
    }


def test_master_row_normalizes_revive_botox_identity_and_price():
    page = _page("[SEGMENT 6]Injectables Botox $11 Per Unit")
    segments = build_segment_records(page)
    offer = {
        "service_name": "Botox",
        "service_category": "Injectables",
        "offer_raw_text": "Injectables Botox $11 Per Unit",
        "original_price": "11",
        "unit_type": "unit",
        "evidence_segments": [6],
    }

    master = build_master_offer_row(offer, page, segments)

    assert master["source_url_normalized"] == "https://revivemedspaokc.com/pricing"
    assert master["raw_service_name"] == "Botox"
    assert master["display_service_name"] == "Botox"
    assert master["canonical_service_name"] == "Botox"
    assert master["service_name"] == "Botox"
    assert master["price_model"] == "per_unit"
    assert master["offer_type"] == "standard_price"
    assert master["price_signature"] == "original_price:11|unit:unit"
    assert master["original_price"] == 11.0
    assert len(master["offer_fingerprint"]) == 64
    assert len(master["evidence_hash"]) == 64


def test_plan_initial_offer_load_links_evidence_and_dedupes_fingerprint():
    page = _page("[SEGMENT 6]Injectables Botox $11 Per Unit")
    segments = build_segment_records(page)
    offer = {
        "service_name": "Botox",
        "offer_raw_text": "Injectables Botox $11 Per Unit",
        "original_price": "11",
        "unit_type": "unit",
        "evidence_segments": [6],
    }

    plan = plan_initial_offer_load(page, [offer, dict(offer)], segments)

    assert plan["summary"] == {
        "offers_input": 2,
        "master_rows": 1,
        "evidence_rows": 1,
        "duplicate_offers": 1,
    }
    evidence = plan["evidence_rows"][0]
    assert evidence["offer_fingerprint"] == plan["master_rows"][0]["offer_fingerprint"]
    assert evidence["segment_identity_hash"] == segments[0]["segment_identity_hash"]
    assert evidence["evidence_role"] == "primary_offer_text"


def test_display_service_can_differ_from_canonical_for_lip_flip():
    page = {
        "promo_website_id": 2,
        "business_id": 8,
        "domain_name": "nakedmd.com",
        "subpage_url": "https://www.nakedmd.com/collections/offers",
        "page_content": "[SEGMENT 2]Lip Flip Regular price $99.00 26% Sale price Regular price $135.00",
    }
    segments = build_segment_records(page)
    offer = {
        "service_name": "Lip Flip",
        "offer_raw_text": "Lip Flip Regular price $99.00 26% Sale price Regular price $135.00",
        "discount_price": "$99.00",
        "original_price": "$135.00",
        "unit_type": "treatment",
        "evidence_segments": [2],
    }

    master = build_master_offer_row(offer, page, segments)

    assert master["display_service_name"] == "Lip Flip"
    assert master["canonical_service_name"] == "Neurotoxin"
    assert master["service_name"] == "Neurotoxin"
    assert master["offer_type"] == "promotion"
    assert master["price_model"] == "fixed_price"
    assert "discount_price:99" in master["price_signature"]


def test_master_row_prefers_display_and_canonical_service_fields():
    page = _page("[SEGMENT 10]Restylane Kysse $650/syringe")
    segments = build_segment_records(page)
    offer = {
        "service_name": "Dermal Filler",
        "display_service_name": "Restylane Kysse",
        "canonical_service_name": "Dermal Filler",
        "offer_raw_text": "Restylane Kysse $650/syringe",
        "original_price": "650",
        "unit_type": "syringe",
        "evidence_segments": [10],
    }

    master = build_master_offer_row(offer, page, segments)

    assert master["raw_service_name"] == "Dermal Filler"
    assert master["display_service_name"] == "Restylane Kysse"
    assert master["canonical_service_name"] == "Dermal Filler"
    assert master["service_name"] == "Dermal Filler"
    assert master["price_model"] == "per_syringe"


def test_inference_helpers_are_stable():
    assert infer_canonical_service_name("Lip Flip") == "Neurotoxin"
    assert infer_offer_type({"membership_price": "199", "offer_raw_text": "$199/month"}) == "membership"
    assert infer_price_model({"unit_type": "syringe"}) == "per_syringe"
    assert build_price_signature({"discount_price": "$99.00", "unit_type": "treatment"}) == "discount_price:99|unit:treatment"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("offer_initial_load tests passed")