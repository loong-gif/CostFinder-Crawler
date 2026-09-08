from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

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


def test_self_hosted_scrape_request_uses_legacy_v1_compatible_fields(monkeypatch) -> None:
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True, "data": {"markdown": "price $12"}}

    def post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.requests.post", post)
    from scripts.firecrawl_self_hosted_monitor import SelfHostedMonitor

    SelfHostedMonitor("http://local", "", 1).scrape("https://example.com")
    assert captured["json"] == {"url": "https://example.com", "formats": ["markdown"], "onlyMainContent": True}


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
