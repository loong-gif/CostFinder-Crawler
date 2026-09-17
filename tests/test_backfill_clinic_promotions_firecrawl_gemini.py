from __future__ import annotations

from scripts.backfill_clinic_promotions_firecrawl_gemini import (
    GEMINI_MODEL,
    SCREENSHOT_FORMAT,
    build_gemini_client,
    select_targets,
    process_target,
)


class FakeDb:
    def __init__(self) -> None:
        self.update_calls: list[dict] = []

    def update_row(self, table, filters, payload):
        self.update_calls.append({"table": table, "filters": filters, "payload": payload})
        return [{"promotion_id": 7, **payload}]


def test_select_targets_accepts_null_and_empty_content_in_id_order() -> None:
    targets = select_targets(
        [
            {"promotion_id": 3, "promotion_content": ["Already filled"]},
            {"promotion_id": 4, "promotion_content": []},
            {"promotion_id": 2, "promotion_content": None},
        ]
    )

    assert [row["promotion_id"] for row in targets] == [2, 4]


def test_screenshot_format_is_full_page_and_desktop_sized() -> None:
    assert SCREENSHOT_FORMAT.type == "screenshot"
    assert SCREENSHOT_FORMAT.full_page is True
    assert SCREENSHOT_FORMAT.viewport == {"width": 1440, "height": 1200}
    assert SCREENSHOT_FORMAT.quality == 80


def test_scrape_target_uses_the_cloud_firecrawl_client(monkeypatch) -> None:
    calls = []

    class CloudClient:
        def scrape(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return {"markdown": "# Special", "screenshot": "https://image.example/shot.png"}

    monkeypatch.setattr(
        "scripts.backfill_clinic_promotions_firecrawl_gemini.get_firecrawl_cloud_client",
        lambda: CloudClient(),
    )
    from scripts.backfill_clinic_promotions_firecrawl_gemini import scrape_target

    response = scrape_target("https://clinic.example/specials")

    assert response["screenshot"] == "https://image.example/shot.png"
    assert calls[0]["formats"][-1] == SCREENSHOT_FORMAT


def test_build_gemini_client_only_reads_gemini_key(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("LLM_API_KEY", "wrong-key")

    client = build_gemini_client()

    assert client.api_key == "gemini-key"
    assert client.model == GEMINI_MODEL


def test_process_target_links_raw_before_writing_validated_content(monkeypatch) -> None:
    db = FakeDb()
    target = {
        "promotion_id": 7,
        "business_id": 101,
        "source_url": "https://clinic.example/specials",
        "promotion_content": None,
    }

    monkeypatch.setattr(
        "scripts.backfill_clinic_promotions_firecrawl_gemini.scrape_target",
        lambda _url: {"success": True, "data": {"markdown": "# Summer Special\nSave $20 on Botox.", "metadata": {"title": "Specials"}, "screenshot": "https://image.example/shot.png"}},
    )
    monkeypatch.setattr(
        "scripts.backfill_clinic_promotions_firecrawl_gemini.save_scrape_response",
        lambda *_args, **_kwargs: [{"id": 88, "screenshot": "https://image.example/shot.png"}],
    )
    monkeypatch.setattr(
        "scripts.backfill_clinic_promotions_firecrawl_gemini.llm_extract_promotions",
        lambda *_args, **_kwargs: {"explanation": "found", "promotions": [{"promotion_title": "Summer Special", "promotion_content": ["Save $20 on Botox."], "campaign_start_date": None, "campaign_end_date": None}]},
    )

    result = process_target(db, object(), target)

    assert result["status"] == "updated"
    assert result["scrape_id"] == 88
    assert db.update_calls[0]["payload"]["scrape_id"] == 88
    assert "promotion_content" not in db.update_calls[0]["payload"]
    assert db.update_calls[1]["payload"]["promotion_content"] == ["Summer Special", "Save $20 on Botox."]


def test_process_target_keeps_content_empty_when_no_validated_promotion(monkeypatch) -> None:
    db = FakeDb()
    target = {
        "promotion_id": 7,
        "business_id": 101,
        "source_url": "https://clinic.example/specials",
        "promotion_content": [],
    }
    monkeypatch.setattr(
        "scripts.backfill_clinic_promotions_firecrawl_gemini.scrape_target",
        lambda _url: {"success": True, "data": {"markdown": "No current offers.", "screenshot": "https://image.example/shot.png"}},
    )
    monkeypatch.setattr(
        "scripts.backfill_clinic_promotions_firecrawl_gemini.save_scrape_response",
        lambda *_args, **_kwargs: [{"id": 88, "screenshot": "https://image.example/shot.png"}],
    )
    monkeypatch.setattr(
        "scripts.backfill_clinic_promotions_firecrawl_gemini.llm_extract_promotions",
        lambda *_args, **_kwargs: {"explanation": "none", "promotions": []},
    )

    result = process_target(db, object(), target)

    assert result["status"] == "no_promotions"
    assert len(db.update_calls) == 1
    assert db.update_calls[0]["payload"]["scrape_id"] == 88


def test_process_target_records_screenshot_missing_and_links_raw(monkeypatch) -> None:
    db = FakeDb()
    target = {
        "promotion_id": 7,
        "business_id": 101,
        "source_url": "https://clinic.example/specials",
        "promotion_content": None,
    }
    monkeypatch.setattr(
        "scripts.backfill_clinic_promotions_firecrawl_gemini.scrape_target",
        lambda _url: {"success": True, "data": {"markdown": "# Special"}},
    )
    saved = {}

    def save(*_args, **kwargs):
        saved.update(kwargs)
        return [{"id": 99}]

    monkeypatch.setattr("scripts.backfill_clinic_promotions_firecrawl_gemini.save_scrape_response", save)

    result = process_target(db, object(), target)

    assert result["status"] == "screenshot_missing"
    assert saved["success"] is False
    assert db.update_calls[0]["payload"]["scrape_id"] == 99
