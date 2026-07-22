"""Tests for search_raw service evidence selection."""
from utils.search_raw_service_evidence import pick_service_evidence_for_business


def test_pick_service_evidence_for_business_calista():
    rows = [
        {
            "id": 14,
            "response_json": [
                {
                    "url": "https://www.calistamedspa.com/masseter-botox-before-and-after-jaw-slimming-results-timeline/",
                    "position": 9,
                    "description": "$12 per unit",
                },
                {
                    "url": "https://www.calistamedspa.com/services/",
                    "position": 10,
                    "description": "0-49 units $13 / unit",
                },
            ],
        }
    ]
    evidence = pick_service_evidence_for_business(rows, website="calistamedspa.com")
    assert evidence is not None
    assert evidence["source_url"].endswith("/services")
    assert evidence["path_score"] >= 30
