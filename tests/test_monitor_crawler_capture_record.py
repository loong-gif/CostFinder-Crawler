from __future__ import annotations

import json

from crawler_monitor_support import (
    QUOTE, artifact, local_firecrawl, monitor_once, page_html, snapshot_pages, without_ego_browser,
)
from utils.monitor_visibility_evidence import CRAWLER_SOURCE


def change_types(state_dir) -> list[str]:
    events = [json.loads(path.read_text()) for path in (state_dir / "events").glob("*.json")]
    return [event["change_type"] for event in sorted(events, key=lambda event: event["created_at"])]


def test_price_change_on_a_crawler_page_is_enqueued_as_a_price_change(tmp_path, css_site, monkeypatch):
    without_ego_browser(monkeypatch)
    url = css_site.add("specials", page_html(f"<p>{QUOTE}</p>"), content_type="text/html")
    firecrawl = local_firecrawl(css_site)
    monitor_once(tmp_path, firecrawl, url, catchup=False)
    assert artifact(tmp_path)["browser_fallback"] is False
    css_site.add("specials", page_html(f"<p>{QUOTE.replace('$300', '$280')}</p>"), content_type="text/html")
    report = monitor_once(tmp_path, firecrawl, url, catchup=False)
    assert report["changed_urls"] == [url] and report["visibility"][0]["visibility_source"] == CRAWLER_SOURCE
    monitor_once(tmp_path, firecrawl, url, catchup=False)
    assert change_types(tmp_path) == ["visible_content_changed", "changed"]
    assert snapshot_pages(tmp_path)["pages"][url]["sync_enqueued"] is True


def test_a_failed_scrape_leaves_the_baseline_incomplete(tmp_path, css_site, monkeypatch):
    without_ego_browser(monkeypatch)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.time.sleep", lambda seconds: None)
    good = css_site.add("specials", page_html(f"<p>{QUOTE}</p>"), content_type="text/html")
    failing = css_site.add("firecrawl-fails", page_html(f"<p>{QUOTE}</p>"), content_type="text/html")
    firecrawl = local_firecrawl(css_site)
    report = monitor_once(tmp_path / "both", firecrawl, good, failing)
    assert [row["url"] for row in report["page_errors"]] == [failing]
    assert snapshot_pages(tmp_path / "both")["baseline_complete"] is False
    monitor_once(tmp_path / "good", firecrawl, good)
    assert snapshot_pages(tmp_path / "good")["baseline_complete"] is True


def test_crawler_evidence_keeps_the_final_address_and_title_after_a_redirect(tmp_path, css_site, monkeypatch):
    without_ego_browser(monkeypatch)
    url = css_site.add("specials", page_html(f"<p>{QUOTE}</p>"), content_type="text/html")
    old = css_site.add("old-specials", url, status=302, content_type="text/html")
    monitor_once(tmp_path, local_firecrawl(css_site), old)
    evidence = artifact(tmp_path)["rendered"]["browser"]
    assert (evidence["url"], evidence["final_url"], evidence["title"]) == (old, url, "Monthly Specials")


def test_a_non_2xx_crawl_is_not_crawler_evidence(tmp_path, css_site, monkeypatch):
    without_ego_browser(monkeypatch)
    url = css_site.add("specials", page_html(f"<p>{QUOTE}</p>"), status=300, content_type="text/html")
    report = monitor_once(tmp_path, local_firecrawl(css_site), url)
    assert report["visibility"][0]["crawler_reasons"] == ["crawl_status_not_ok"]
