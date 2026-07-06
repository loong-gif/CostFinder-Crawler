from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_evidence_segments import (
    build_segment_records,
    normalize_url,
    parse_page_segments,
    parse_segment,
    split_page_content,
)


def test_normalize_url_strips_www_tracking_and_trailing_slash():
    assert normalize_url("http://www.Example.com/specials/?utm_source=x&keep=1#top") == (
        "https://example.com/specials?keep=1"
    )


def test_split_page_content_reads_segment_markers():
    page = "[SEGMENT 6]\nInjectables Botox $11 Per Unit\n\n[SEGMENT 7]\nDysport $3.70 Per Unit"
    assert split_page_content(page) == [
        (6, "Injectables Botox $11 Per Unit"),
        (7, "Dysport $3.70 Per Unit"),
    ]


def test_revive_price_row_extracts_service_price_unit_terms():
    segment = parse_segment(
        6,
        "Injectables Botox $11 Per Unit",
        source_url_normalized="https://revivemedspaokc.com/pricing",
    )
    assert segment.segment_type == "price_row"
    assert segment.price_values == [11.0]
    assert segment.service_mentions == ["Botox"]
    assert "per_unit" in segment.offer_terms
    assert segment.is_offer_signal is True
    assert segment.content_quality_score >= 0.8


def test_semantic_hash_changes_when_price_changes_but_identity_stays():
    old = parse_segment(6, "Injectables Botox $11 Per Unit", source_url_normalized="https://x.test/pricing")
    new = parse_segment(6, "Injectables Botox $12 Per Unit", source_url_normalized="https://x.test/pricing")
    assert old.semantic_hash != new.semantic_hash
    assert old.segment_identity_hash == new.segment_identity_hash


def test_deluxe_morpheus8_regular_and_first_time_segments_keep_offer_terms():
    regular = parse_segment(
        0,
        "SPECIAL OFFER One Morpheus 8 RF MicroNeedling Treatment For Full Face: Regular Price: $1600",
    )
    first_time = parse_segment(
        1,
        "One Morpheus 8 RF MicroNeedling Treatment For Full Face: First Time Patient Price: $1199",
    )
    assert regular.service_mentions == ["Morpheus8", "Microneedling"]
    assert regular.price_values == [1600.0]
    assert "special" in regular.offer_terms
    assert first_time.price_values == [1199.0]
    assert "first_time_patient" in first_time.offer_terms


def test_nakedmd_product_collection_keeps_display_level_service_hints():
    segment = parse_segment(
        2,
        "Lip Flip Regular price $99.00 $99.00 26% Sale price Regular price $135.00 Unit price / per",
    )
    assert "Lip Flip" in segment.service_mentions
    assert segment.price_values == [99.0, 99.0, 135.0]
    assert "percent_discount" in segment.offer_terms
    assert segment.is_offer_signal is True


def test_build_segment_records_matches_sql_payload_shape():
    row = {
        "promo_website_id": 1863,
        "business_id": 123,
        "subpage_url": "https://www.revivemedspaokc.com/pricing/",
        "page_content": "[SEGMENT 6]\nInjectables Botox $11 Per Unit",
    }
    records = build_segment_records(row)
    assert len(records) == 1
    record = records[0]
    assert record["promo_website_id"] == 1863
    assert record["business_id"] == 123
    assert record["source_url_normalized"] == "https://revivemedspaokc.com/pricing"
    assert record["segment_index"] == 6
    assert record["service_mentions"] == ["Botox"]
    assert record["price_values"] == [11.0]
