from datetime import date

from scripts.audit_promotion_consistency import consistency_counts


def test_consistency_counts_detects_each_lifecycle_violation():
    promotions = [
        {"promotion_id": 1, "is_active": True, "campaign_end_date": "2026-09-18", "is_offer_extracted": False, "is_ocr_required": False, "promotion_content": ["x"]},
        {"promotion_id": 2, "is_active": False, "campaign_end_date": None, "is_offer_extracted": True, "is_ocr_required": None, "promotion_content": []},
        {"promotion_id": 3, "is_active": False, "campaign_end_date": None, "is_offer_extracted": False, "is_ocr_required": False, "promotion_content": ["x"]},
    ]
    offers = [
        {"id": 10, "promotion_id": 1, "is_active": True, "offer_fingerprint": "dup"},
        {"id": 11, "promotion_id": 2, "is_active": True, "offer_fingerprint": "dup"},
        {"id": 12, "promotion_id": 3, "is_active": True, "offer_fingerprint": "other"},
    ]

    report = consistency_counts(promotions, offers, as_of=date(2026, 9, 19))

    assert report["is_offer_extracted_false_with_offer"] == [1, 3]
    assert report["is_offer_extracted_true_without_offer"] == []
    assert report["active_promotion_expired"] == [1]
    assert report["empty_promotion_content"] == [2]
    assert report["null_is_ocr_required"] == [2]
    assert report["inactive_promotion_with_active_offer"] == [2, 3]
    assert report["active_offer_fingerprint_duplicates"] == {"dup": [10, 11]}


def test_consistency_counts_accepts_clean_state():
    promotions = [
        {"promotion_id": 1, "is_active": True, "campaign_end_date": "2026-09-30", "is_offer_extracted": True, "is_ocr_required": False, "promotion_content": ["x"]},
        {"promotion_id": 2, "is_active": False, "campaign_end_date": "2026-09-01", "is_offer_extracted": False, "is_ocr_required": False, "promotion_content": ["historical"]},
    ]
    offers = [{"id": 10, "promotion_id": 1, "is_active": True, "offer_fingerprint": "one"}]

    report = consistency_counts(promotions, offers, as_of=date(2026, 9, 19))

    assert report["is_offer_extracted_false_with_offer"] == []
    assert report["is_offer_extracted_true_without_offer"] == []
    assert report["active_promotion_expired"] == []
    assert report["empty_promotion_content"] == []
    assert report["null_is_ocr_required"] == []
    assert report["inactive_promotion_with_active_offer"] == []
    assert report["active_offer_fingerprint_duplicates"] == {}
