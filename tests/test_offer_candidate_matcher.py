from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_candidate_matcher import rank_offer_candidates, to_match_candidate_records
from utils.offer_evidence_segments import build_segment_records


def _segment(text: str):
    row = {
        "promo_website_id": 1,
        "subpage_url": "https://www.revivemedspaokc.com/pricing",
        "page_content": f"[SEGMENT 6]{text}",
    }
    return build_segment_records(row)[0]


def test_revive_botox_segment_matches_exact_offer_first():
    segment = _segment("Injectables Botox $11 Per Unit")
    candidates = [
        {
            "id": "dysport",
            "source_url": "https://www.revivemedspaokc.com/pricing",
            "service_name": "Dysport",
            "offer_raw_text": "Dysport $3.70 Per Unit",
            "regular_price": 3.7,
            "unit_type": "unit",
        },
        {
            "id": "botox",
            "source_url": "https://revivemedspaokc.com/pricing/",
            "service_name": "Botox",
            "offer_raw_text": "Botox $11 Per Unit",
            "regular_price": 11,
            "unit_type": "unit",
        },
    ]
    matches = rank_offer_candidates(segment, candidates)
    assert matches[0].candidate_offer_id == "botox"
    assert matches[0].match_method == "url_service_price"
    assert matches[0].score_breakdown["url"] == 1.0
    assert matches[0].score_breakdown["price"] == 1.0


def test_price_change_still_matches_same_identity_without_exact_price():
    segment = _segment("Injectables Botox $12 Per Unit")
    candidates = [
        {
            "id": "botox",
            "source_url": "https://revivemedspaokc.com/pricing",
            "service_name": "Botox",
            "offer_raw_text": "Botox $11 Per Unit",
            "regular_price": 11,
            "unit_type": "unit",
        }
    ]
    matches = rank_offer_candidates(segment, candidates, min_score=0.5)
    assert matches[0].candidate_offer_id == "botox"
    assert matches[0].match_method == "url_service"
    assert matches[0].score_breakdown["price"] == 0.0
    assert matches[0].match_score >= 0.7


def test_nakedmd_display_service_beats_generic_canonical_candidate():
    row = {
        "promo_website_id": 2,
        "subpage_url": "https://www.nakedmd.com/collections/offers",
        "page_content": "[SEGMENT 2]Lip Flip Regular price $99.00 26% Sale price Regular price $135.00",
    }
    segment = build_segment_records(row)[0]
    candidates = [
        {
            "id": "generic-tox",
            "source_url": "https://www.nakedmd.com/collections/offers",
            "service_name": "Neurotoxin",
            "canonical_service_name": "Neurotoxin",
            "offer_raw_text": "Naked TOX | Neurotoxins Regular price From $69.90",
            "discount_price": 69.9,
        },
        {
            "id": "lip-flip",
            "source_url": "https://nakedmd.com/collections/offers/",
            "display_service_name": "Lip Flip",
            "canonical_service_name": "Neurotoxin",
            "offer_raw_text": "Lip Flip Regular price $99.00 26% Sale price Regular price $135.00",
            "regular_price": 135,
            "discount_price": 99,
        },
    ]
    matches = rank_offer_candidates(segment, candidates)
    assert matches[0].candidate_offer_id == "lip-flip"
    assert matches[0].match_score > matches[1].match_score


def test_to_match_candidate_records_matches_schema_shape():
    segment = _segment("Injectables Botox $11 Per Unit")
    matches = rank_offer_candidates(
        segment,
        [
            {
                "id": "botox",
                "source_url": "https://revivemedspaokc.com/pricing",
                "service_name": "Botox",
                "offer_raw_text": "Botox $11 Per Unit",
                "regular_price": 11,
                "unit_type": "unit",
            }
        ],
    )
    records = to_match_candidate_records("seg-1", "event-1", matches)
    assert records == [
        {
            "change_event_id": "event-1",
            "segment_id": "seg-1",
            "candidate_offer_id": "botox",
            "match_score": matches[0].match_score,
            "match_method": "url_service_price",
            "score_breakdown": matches[0].score_breakdown,
            "rank": 1,
            "is_selected": False,
        }
    ]
