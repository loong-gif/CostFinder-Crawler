from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawler_monitor_support import (
    QUOTE, artifact, local_firecrawl, local_llm, monitor_once, page_html, without_ego_browser,
)
from monitor_price_plan_support import _graph, _with_extra_offer
from scripts.run_monitor_price_sync import process_events
from utils.monitor_visibility_evidence import CRAWLER_SOURCE, CrawlerVisibility, coverage_complete, visibility_source

SERVICE_NAMES = ["Botox", "Dermal Filler", "Dysport", "Others"]
ANSWER = {"offers": [{"offer_raw_text": QUOTE, "regular_price": 450, "discount_price": 300, "discount_percent": None,
                      "discount_amount": None, "price_model": "total", "is_package": False,
                      "is_membership_required": False, "is_new_customer_required": False, "quantity_kind": "fixed",
                      "conditions": [], "items": [{"item_name": "Botox", "service_id": 12, "quantity": 50,
                                                   "unit_price": None, "service_area": None, "unit_type": "units"}]}],
          "all_offers_extracted": True, "no_current_offers_quote": None, "promotion_title": None,
          "campaign_start_date": None, "campaign_end_date": None}


def snapshot_for(url: str, site_base: str) -> dict:
    """数据库里这个页面有两条有效 offer：Botox $350 与页面上已经没有的 Dysport $200。"""
    graph = _with_extra_offer(_graph())
    for promotion in graph["promotions"]:
        promotion["source_url"] = url
    return {**graph, "businesses": [{"business_id": 7, "website": site_base}], "memberships": [], "terms": [],
            "service_names": SERVICE_NAMES, "confirmations": [], "audit": []}


def test_clean_text_page_is_processed_and_planned_from_crawler_content_without_a_render(tmp_path, css_site,
                                                                                      monkeypatch):
    without_ego_browser(monkeypatch)
    css_site.add("site.css", "p{color:#333}")
    url = css_site.add("specials", page_html(f"<p>{QUOTE}</p>", '<link rel="stylesheet" href="site.css">'),
                       content_type="text/html")
    received = local_llm(css_site, monkeypatch, ANSWER)
    report = monitor_once(tmp_path, local_firecrawl(css_site), url)
    assert not (tmp_path / "rendered").exists()
    row = report["visibility"][0]
    assert (row["visibility_source"], row["accessible"], row["complete"], row["crawler_reasons"],
            row["browser_path"]) == (CRAWLER_SOURCE, True, True, [], None)
    evidence = artifact(tmp_path)["rendered"]["browser"]
    assert evidence["visibility_source"] == CRAWLER_SOURCE and QUOTE in evidence["text"]
    assert "rawHtml" not in artifact(tmp_path)["raw_response"]["data"]
    assert json.loads((tmp_path / "daily.json").read_text())["pages"][url]["visibility_source"] == CRAWLER_SOURCE

    result = process_events(tmp_path, snapshot_for(url, css_site.base))
    assert not (tmp_path / "rendered").exists() and not list(tmp_path.glob("sync/*/desktop"))
    assert [row["status"] for row in result["sources"]] == ["pending_sql"], result["sources"]
    assert QUOTE in received[0][1]["content"]
    assert json.loads(next(tmp_path.glob("sync/*/browser.json")).read_text())["visibility_source"] == CRAWLER_SOURCE
    sql = Path(result["sources"][0]["sql_path"]).read_text()
    assert f"update public.promo_offer_master set discount_price = 300, offer_raw_text = '{QUOTE}' where id = 4;" in sql
    assert "update public.promo_offer_master set is_active = false where id = 6;" in sql


def test_promotion_hidden_by_the_site_stylesheet_is_not_planned_from_the_hidden_text(tmp_path, css_site, monkeypatch):
    without_ego_browser(monkeypatch)
    css_site.add("site.css", ".old{display:none}")
    hidden = "Dysport 100 units Special $200"
    url = css_site.add("specials", page_html(f'<p>{QUOTE}</p><p class="old">{hidden}</p>',
                                             '<link rel="stylesheet" href="site.css">'), content_type="text/html")
    received = local_llm(css_site, monkeypatch, ANSWER)
    report = monitor_once(tmp_path, local_firecrawl(css_site), url)
    assert hidden in artifact(tmp_path)["markdown"]
    assert report["visibility"][0]["visibility_source"] == "browser_render"
    assert "claim_not_in_visible_text" in report["visibility"][0]["crawler_reasons"]
    assert len(list((tmp_path / "rendered").iterdir())) == 1

    result = process_events(tmp_path, snapshot_for(url, css_site.base))
    assert [row["status"] for row in result["sources"]] == ["retryable"]
    assert received == []


@pytest.mark.parametrize("body, reason", [
    ('<div class="swiper"><p>{quote}</p></div>', "js_widget_state"),
    ('<p>{quote}</p><img src="botox-special.jpg" alt="Botox special $9 per unit">', "image_promo_candidate"),
])
def test_page_the_crawler_cannot_judge_is_rendered(tmp_path, css_site, monkeypatch, body, reason):
    without_ego_browser(monkeypatch)
    url = css_site.add("specials", page_html(body.format(quote=QUOTE)), content_type="text/html")
    report = monitor_once(tmp_path, local_firecrawl(css_site), url)
    assert reason in report["visibility"][0]["crawler_reasons"]
    assert report["visibility"][0]["visibility_source"] == "browser_render"
    assert (next((tmp_path / "rendered").iterdir()) / "browser.json").is_file()


@pytest.mark.parametrize("path, status, reason", [
    ("collections/botox/products/botox-50", 200, "product_variants_require_browser"),
    ("specials", 404, "crawl_status_not_ok"),
])
def test_product_and_gone_pages_keep_the_browser(tmp_path, css_site, monkeypatch, path, status, reason):
    without_ego_browser(monkeypatch)
    url = css_site.add(path, page_html(f"<p>{QUOTE}</p>"), status=status, content_type="text/html")
    report = monitor_once(tmp_path, local_firecrawl(css_site), url)
    row = report["visibility"][0]
    assert row["crawler_reasons"] == [reason] and row["accessible"] is False and row["complete"] is False
    assert (tmp_path / "rendered").is_dir()


def events(state_dir: Path) -> list[str]:
    return sorted(json.loads(path.read_text())["change_type"] for path in (state_dir / "events").glob("*.json"))


def test_unchanged_clean_page_enqueues_nothing_on_the_next_capture(tmp_path, css_site, monkeypatch):
    without_ego_browser(monkeypatch)
    url = css_site.add("specials", page_html(f"<p>{QUOTE}</p>"), content_type="text/html")
    firecrawl = local_firecrawl(css_site)
    monitor_once(tmp_path, firecrawl, url)
    report = monitor_once(tmp_path, firecrawl, url)
    assert report["visibility"][0]["visibility_source"] == CRAWLER_SOURCE
    assert events(tmp_path) == ["catchup"] and not (tmp_path / "rendered").exists()


def test_page_whose_render_fails_asks_for_a_new_browser_capture_every_run(tmp_path, css_site, monkeypatch):
    without_ego_browser(monkeypatch)
    url = css_site.add("specials", page_html(f'<div class="swiper"><p>{QUOTE}</p></div>'), content_type="text/html")
    firecrawl = local_firecrawl(css_site)
    monitor_once(tmp_path, firecrawl, url)
    report = monitor_once(tmp_path, firecrawl, url)
    assert report["visibility"][0]["accessible"] is False
    assert events(tmp_path) == ["browser_capture_required", "catchup"]


def crawler_evidence(**changes) -> dict:
    return {**CrawlerVisibility(url="https://a.test/specials", final_url="https://a.test/specials", http_status=200,
                                title="Specials", text=QUOTE, context_digest="d",
                                captured_at="2026-11-11T00:00:00+00:00").as_json(), **changes}


def test_crawler_evidence_is_complete_desktop_coverage_without_browser_artifacts():
    assert visibility_source(crawler_evidence()) == CRAWLER_SOURCE
    assert coverage_complete(crawler_evidence())
    assert crawler_evidence()["image_review_required"] is False


@pytest.mark.parametrize("changes", [
    {"image_tiles": {"a": [{"path": "a.png"}], "b": []}}, {"screenshot_path": "s.png"},
    {"image_review_required": True}, {"breakpoint_coverage": {"desktop": {"complete": True}}},
    {"variant_captures": [{"screenshot_path": "v.png"}]}, {"accessible": False},
])
def test_crawler_evidence_never_carries_browser_artifacts_or_image_review(changes):
    with pytest.raises(ValueError, match="invalid_crawler_visibility"):
        visibility_source(crawler_evidence(**changes))


def test_unknown_visibility_source_is_rejected():
    with pytest.raises(ValueError, match="unknown_visibility_source"):
        visibility_source({"visibility_source": "markdown"})


def test_browser_coverage_must_cover_every_breakpoint_completely():
    browser = {"visibility_source": "browser_render",
               "breakpoint_coverage": {"desktop": {"complete": True, "screenshot_path": "s.png"}}}
    assert coverage_complete(browser)
    assert not coverage_complete({**browser, "breakpoint_coverage": {"desktop": {"complete": False}}})
    assert not coverage_complete({**browser, "breakpoint_coverage": {"mobile": {"complete": True}}})
    assert not coverage_complete({"visibility_source": "browser_render"})
