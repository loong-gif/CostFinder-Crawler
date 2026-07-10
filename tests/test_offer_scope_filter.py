"""Tests for offer_scope_filter."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.change_driven_extractor import apply_offer_actions
from utils.offer_extraction_llm import normalize_offer_payload
from utils.offer_scope_filter import (
    filter_service_offers,
    is_consultation_offer,
    is_membership_plan_offer,
    should_exclude_from_offer_master,
)


def test_consultation_offer_detected():
    assert is_consultation_offer({"service_name": "Free Consultation", "offer_raw_text": "Free Consultation $0"})
    assert is_consultation_offer({"service_name": "Botox", "offer_raw_text": "Free 15 Minute Injectable Consultation $0"})
    assert not is_consultation_offer({"service_name": "Botox", "offer_raw_text": "Botox $11/unit"})


def test_membership_plan_keeps_linked_service_price():
    assert not is_membership_plan_offer(
        {"service_name": "Botox", "membership_plan_id": 42, "discount_price": 11, "offer_raw_text": "Botox $11/unit"}
    )
    assert is_membership_plan_offer({"service_name": "Membership", "offer_raw_text": "VIP $199/month"})


def test_normalize_offer_payload_filters_non_service():
    payload = normalize_offer_payload(
        {
            "offers": [
                {"service_name": "Botox", "offer_raw_text": "Botox $11/unit", "evidence_segments": [1]},
                {"service_name": "Free Consultation", "offer_raw_text": "Free Consultation", "evidence_segments": [2]},
            ]
        },
        allowed_indexes={1, 2},
    )
    assert len(payload["offers"]) == 1
    assert payload["offers"][0]["service_name"] == "Botox"


def test_apply_offer_actions_skips_consultation_insert():
    class _Client:
        def insert_rows(self, *args, **kwargs):
            raise AssertionError("should not insert")

    result = apply_offer_actions(
        _Client(),
        [{"action": "insert", "service_name": "Free Consultation", "offer_raw_text": "Free Consultation $0"}],
        source_url="https://example.com/pricing",
        source_name="example.com",
        dry_run=True,
    )
    assert result["inserted"] == 0
    assert result["skipped"] == 1


def test_filter_service_offers():
    rows = filter_service_offers(
        [
            {"service_name": "Membership", "offer_raw_text": "$99/month"},
            {"service_name": "Skincare Product", "source_url": "https://x.com/collections", "offer_raw_text": "$9"},
            {"service_name": "Dysport", "offer_raw_text": "$10/unit"},
        ]
    )
    assert len(rows) == 1
    assert rows[0]["service_name"] == "Dysport"
    assert should_exclude_from_offer_master({"service_name": "Membership", "offer_raw_text": "$99/month"})
