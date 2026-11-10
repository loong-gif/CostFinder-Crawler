from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.firecrawl_self_hosted_monitor import (
    build_events,
    build_snapshot,
    extract_price_facts,
    response_markdown,
    run_monitor,
)


def test_extract_price_facts_ignores_non_price_copy_changes() -> None:
    before = extract_price_facts("""New navigation\nBotox special: $12/unit\nBook now""")
    after = extract_price_facts("""Different navigation\nBotox special: $14/unit\nBook today""")
    assert before == ("botox special: $12/unit",)
    assert after == ("botox special: $14/unit",)


def test_baseline_has_no_events() -> None:
    page = Mock(url="https://example.com", status="new", facts=("botox: $12/unit",), error=None)
    snapshot = build_snapshot("daily_01", [page], baseline=True, baseline_complete=True)
    assert snapshot["baseline"] is True
    assert snapshot["baseline_complete"] is True
    assert build_events("daily_01", [page], {}, baseline=True) == []


def test_price_change_emits_added_and_removed_facts() -> None:
    page = Mock(url="https://example.com", status="changed", facts=("botox: $14/unit",), error=None)
    events = build_events("daily_01", [page], {"https://example.com": {"facts": ["botox: $12/unit"]}}, baseline=False)
    assert events[0]["price_diff"] == {"added": ["botox: $14/unit"], "removed": ["botox: $12/unit"]}


def test_response_markdown_supports_v1_envelope() -> None:
    assert response_markdown({"success": True, "data": {"markdown": "price $12"}}) == "price $12"


CLOUDFLARE_BLOCK = "Please enable cookies.\n\nSorry, you have been blocked\n============================\n"


class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code, self.payload, self.ok = status_code, payload, status_code < 400
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


def _monitor(monkeypatch, response: _Response, rendered: list[str], *, ego_error: str | None = None):
    from scripts.firecrawl_self_hosted_monitor import SelfHostedMonitor
    from utils.ego_browser import BrowserScrapeFailed

    captured = {}

    def post(url, **kwargs):
        captured.update(kwargs)
        return response

    def ego(url):
        rendered.append(url)
        if ego_error:
            raise BrowserScrapeFailed(ego_error)
        return {"markdown": "# Promotions\n\nBotox $10 per unit", "metadata": {"engine": "ego"}}

    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.requests.post", post)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.ego_scrape", ego)
    return SelfHostedMonitor("http://local", "", 1), captured


def test_monitor_scrapes_with_fast_mode_only(monkeypatch) -> None:
    rendered: list[str] = []
    monitor, captured = _monitor(monkeypatch, _Response(200, {"success": True, "data": {
        "markdown": "price $12", "metadata": {"statusCode": 200}}}), rendered)
    assert monitor.scrape("https://example.com") == "price $12"
    assert captured["json"] == {"url": "https://example.com", "formats": ["markdown"], "onlyMainContent": True,
                                "excludeTags": ["nav", "footer", "script", "style", "noscript"], "fastMode": True}
    assert rendered == []


def test_monitor_renders_with_ego_when_fast_mode_has_no_content(monkeypatch) -> None:
    cases = [
        _Response(408, {"success": False, "code": "SCRAPE_TIMEOUT"}),
        _Response(200, {"success": True, "data": {"markdown": CLOUDFLARE_BLOCK, "metadata": {"statusCode": 403}}}),
        _Response(200, {"success": True, "data": {"markdown": "  ", "metadata": {"statusCode": 200}}}),
    ]
    for response in cases:
        rendered: list[str] = []
        monitor, _ = _monitor(monkeypatch, response, rendered)
        payload = monitor.scrape_document("https://www.laseraway.com/promotions/")
        assert payload["data"]["metadata"]["engine"] == "ego"
        assert response_markdown(payload) == "# Promotions\n\nBotox $10 per unit"
        assert rendered == ["https://www.laseraway.com/promotions/"]


def test_monitor_keeps_a_404_as_the_page_result_without_rendering(monkeypatch) -> None:
    rendered: list[str] = []
    gone = {"success": True, "data": {"markdown": "# Page not found", "metadata": {"statusCode": 404}}}
    monitor, _ = _monitor(monkeypatch, _Response(200, gone), rendered)
    assert monitor.scrape_document("https://clinic.example/old-specials") == gone
    assert rendered == []


def test_monitor_records_both_engines_when_ego_also_fails(monkeypatch) -> None:
    from scripts.firecrawl_self_hosted_monitor import ScrapeFailure

    rendered: list[str] = []
    monitor, _ = _monitor(monkeypatch, _Response(408, {"success": False, "code": "SCRAPE_TIMEOUT"}), rendered,
                          ego_error="ego_timeout")
    with pytest.raises(ScrapeFailure, match="ego_scrape_failed") as failure:
        monitor.scrape_document("https://slow.example/specials")
    assert failure.value.raw_response == {
        "fast_mode": {"http_status": 408, "body": {"success": False, "code": "SCRAPE_TIMEOUT"}}, "ego_error": "ego_timeout"}


def test_runner_baselines_without_webhook(tmp_path: Path, monkeypatch) -> None:
    class FakeMonitor:
        def __init__(self, *args, **kwargs):
            pass

        def scrape(self, url: str) -> str:
            return "Botox special: $12/unit"

    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor", FakeMonitor)
    config = {"monitors": {"daily_01": {"targets": [{"urls": ["https://example.com"]}], "webhook": {"url": "https://webhook.invalid"}}}}
    report = run_monitor(config, "daily_01", api_url="http://local", api_key="", state_dir=tmp_path, timeout=1, limit=None, workers=1, send_notifications=False)
    assert report["baseline"] is True
    assert report["meaningful_events"] == 0
    assert json.loads((tmp_path / "daily_01.json").read_text())["pages"]["https://example.com"]["facts"] == ["botox special: $12/unit"]
