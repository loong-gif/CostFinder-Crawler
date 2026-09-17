from scripts.backfill_promotion_screenshot_ocr import (
    content_has_verbatim_screen_evidence,
    fallback_promotion_from_transcript,
    process_target,
    transcript_lines,
)


class FakeDb:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update_row(self, table, filters, payload):
        self.calls.append({"table": table, "filters": filters, "payload": payload})
        return [{"promotion_id": 686, **payload}]


def test_transcript_lines_normalize_and_dedupe():
    assert transcript_lines({"transcript": [" Save $20 ", "save $20", "", 42]}) == ["Save $20", "42"]


def test_screen_evidence_requires_each_complete_content_segment():
    evidence = "Summer Special\nSave $20 on Botox"
    assert content_has_verbatim_screen_evidence(["Summer Special", "Save $20 on Botox"], evidence)
    assert not content_has_verbatim_screen_evidence(["Save $25 on Botox"], evidence)


def test_fallback_requires_a_literal_offer_title_and_price_signal():
    assert fallback_promotion_from_transcript(["New Year Offer Massage and Spa", "Get 50% Off"]) == {
        "promotion_title": "New Year Offer Massage and Spa",
        "promotion_content": ["Get 50% Off"],
        "campaign_start_date": None,
        "campaign_end_date": None,
    }
    assert fallback_promotion_from_transcript(["New Year Offer", "Book now"]) is None


def test_process_target_writes_ocr_before_validated_promotion(monkeypatch):
    db = FakeDb()
    target = {
        "promotion_id": 686,
        "source_url": "https://clinic.example/specials",
        "scrape_id": 1764,
        "raw": {"id": 1764, "success": True, "screenshot": "https://image.example/shot.png"},
    }
    monkeypatch.setattr(
        "scripts.backfill_promotion_screenshot_ocr.download_screenshot",
        lambda _url: (b"image", "image/png"),
    )
    monkeypatch.setattr(
        "scripts.backfill_promotion_screenshot_ocr.extract_verified_content",
        lambda *_args, **_kwargs: (["Summer Special", "Save $20 on Botox"], ["Summer Special", "Save $20 on Botox"]),
    )

    result = process_target(db, object(), target, schema={}, apply=True)

    assert result["status"] == "updated"
    assert db.calls[0]["table"] == "firecrawl_scrape_raw"
    assert db.calls[0]["payload"]["markdown_ocr"] == "Summer Special\nSave $20 on Botox"
    assert db.calls[1]["table"] == "clinic_promotions"
    assert db.calls[1]["payload"]["promotion_content"] == ["Summer Special", "Save $20 on Botox"]
