"""监控事件只在证据无法判定时进入审查；其余重试、重新抓取或排除（用户裁定 2026-10-15、2026-11-07）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import requests

import scripts.run_monitor_price_sync as runner
from utils.monitor_price_retry import RetryLimits
from utils.monitor_price_store import atomic_json
from utils.monitor_rendered_checks import confirmed_not_found
from utils.monitor_review_policy import event_status
from utils.monitor_source_binding import bind_source, not_found_plan

URL = "https://a.test/specials"
SNAPSHOT = {"promotions": [], "offers": [], "items": [], "services": [], "terms": [], "memberships": [],
            "businesses": [{"business_id": 1, "website": "a.test"}], "service_names": ["Botox", "Others"]}


def event(tmp_path, event_id="1", url=URL):
    artifact = tmp_path / "evidence" / f"{event_id}.json"
    atomic_json(artifact, {"url": url, "captured_at": datetime.now(timezone.utc).isoformat(), "markdown": "Botox $12"})
    path = tmp_path / "events" / f"{event_id}.json"
    atomic_json(path, {"event_id": event_id, "status": "pending", "url": url, "artifact_path": str(artifact),
                       "created_at": datetime.now(timezone.utc).isoformat()})
    return path


@pytest.mark.parametrize("review,expected", [
    ([{"reason": "inaccessible_empty_or_unresolved_evidence", "blocking": True}], ("retryable", True)),
    ([{"reason": "unsupported_price_evidence", "blocking": False}], ("retryable", True)),
    ([{"reason": "unsupported_price_evidence", "blocking": False},
      {"reason": "ambiguous_offer_match", "blocking": False}], ("review", False)),
    ([{"reason": "protected_row_or_cascade", "blocking": True}], ("review", False)),
    ([{"reason": "SSLError", "transient": True}], ("retryable", False)),
    ([{"reason": "source_business_missing"}], ("excluded", False)),
    ([{"reason": "KeyError"}], ("review", False)),
])
def test_only_undecidable_reasons_reach_review(review, expected):
    assert event_status({"status": "review", "review": review}) == expected


def test_blocked_or_empty_page_keeps_live_rows_and_is_recaptured(tmp_path, monkeypatch):
    path = event(tmp_path)
    monkeypatch.setattr(runner, "render_page", lambda url, *a, **k: {"accessible": True, "final_url": url})
    monkeypatch.setattr(runner, "extract_evidence", lambda *a: {"url": URL, "accessible": True, "markdown": "", "offers": []})
    snapshot = {**SNAPSHOT, "promotions": [{"promotion_id": 3, "business_id": 1, "source_url": URL, "is_active": True}]}
    plan = runner.prepare_event(path, snapshot, {}, tmp_path)
    assert plan["operations"] == [] and plan["review"][-1]["reason"] == "inaccessible_empty_or_unresolved_evidence"
    saved = json.loads(path.read_text())
    assert saved["status"] == "retryable" and saved["retry_reason"] == "fresh_scrape_required"
    assert saved["fresh_evidence_after"]


def test_network_errors_such_as_ssl_failures_are_retried(tmp_path, monkeypatch):
    path = event(tmp_path)
    monkeypatch.setattr(runner, "render_page", lambda url, *a, **k: {"accessible": True, "final_url": url})
    def fail(*args):
        raise requests.exceptions.SSLError("certificate verify failed")
    monkeypatch.setattr(runner, "extract_evidence", fail)
    runner.prepare_event(path, SNAPSHOT, {}, tmp_path)
    assert json.loads(path.read_text())["status"] == "retryable"


def test_paused_source_stays_retryable_and_is_not_queued_for_review(tmp_path, monkeypatch):
    path = event(tmp_path)
    monkeypatch.setattr(runner, "render_page", lambda *a, **k: {"accessible": False})
    for _ in range(4):
        report = runner.process_events(tmp_path, SNAPSHOT)
    assert RetryLimits(tmp_path).paused("processing", URL)
    assert json.loads(path.read_text())["status"] == "retryable"
    assert all(row["status"] != "review" for row in report["sources"])
    paused = RetryLimits(tmp_path).paused_sources()
    assert paused[0]["url"] == URL and "review" not in paused[0]


def test_gate_evidence_problems_are_recaptured_not_reviewed(tmp_path, monkeypatch):
    path = event(tmp_path)
    monkeypatch.setattr(runner, "prepare_event", lambda *a: {"status": "ready", "operations": [], "review": []})
    monkeypatch.setattr(runner, "render_pending", lambda *a: {"status": "review", "category": "evidence_expired"})
    report = runner.process_events(tmp_path, SNAPSHOT)
    saved = json.loads(path.read_text())
    assert saved["status"] == "retryable" and saved["retry_reason"] == "fresh_scrape_required"
    assert report["sources"][0]["status"] == "retryable"


def test_not_found_page_without_a_live_promotion_is_a_no_op_and_several_are_all_retired():
    graph = {"promotions": [], "offers": [], "items": []}
    assert not_found_plan({"business_id": 1, "url": URL}, graph, {})["operations"] == []
    graph = {"promotions": [{"promotion_id": 3, "is_active": True}, {"promotion_id": 9, "is_active": True}],
             "offers": [{"id": 4, "promotion_id": 9, "is_active": True}], "items": []}
    operations = not_found_plan({"business_id": 1, "url": URL}, graph, {})["operations"]
    assert {(op["table"], op["id"]) for op in operations} == {
        ("clinic_promotions", 3), ("clinic_promotions", 9), ("promo_offer_master", 4)}


def test_http_404_with_a_rendered_screenshot_confirms_removal(tmp_path):
    shot = tmp_path / "screenshot.png"
    shot.write_bytes(b"png")
    browser = {"http_status": 404, "scroll_complete": True, "screenshot_path": str(shot), "text": "Oops! Nothing here."}
    assert confirmed_not_found(browser)
    assert not confirmed_not_found({**browser, "http_status": 200})


def test_redirect_to_the_business_own_website_is_followed_and_third_party_is_reviewed(tmp_path, monkeypatch):
    snapshot = {**SNAPSHOT, "businesses": [{"business_id": 1, "website": "https://new-clinic.test"}],
                "promotions": [{"promotion_id": 3, "business_id": 1, "source_url": URL, "is_active": True}]}
    path = event(tmp_path)
    monkeypatch.setattr(runner, "render_page", lambda *a, **k: {"accessible": True, "final_url": "https://new-clinic.test/specials"})
    monkeypatch.setattr(runner, "extract_evidence", lambda *a: {})
    monkeypatch.setattr(runner, "build_plan", lambda *a: {"status": "ready", "operations": [], "review": []})
    assert runner.prepare_event(path, snapshot, {}, tmp_path)["status"] == "ready"
    path = event(tmp_path, "2")
    monkeypatch.setattr(runner, "render_page", lambda *a, **k: {"accessible": True, "final_url": "https://booking.test/x"})
    assert runner.prepare_event(path, snapshot, {}, tmp_path)["review"] == [{"reason": "source_redirect_requires_review"}]
    assert json.loads(path.read_text())["status"] == "review"


def test_businesses_with_their_own_rows_on_a_shared_page_each_get_a_plan():
    snapshot = {**SNAPSHOT, "businesses": [{"business_id": 1, "website": "a.test"}, {"business_id": 2, "website": "a.test"}],
                "promotions": [{"business_id": 1, "source_url": URL}, {"business_id": 2, "source_url": URL}]}
    assert bind_source(URL, snapshot) == [{"business_id": 1, "url": URL}, {"business_id": 2, "url": URL}]


def test_daily_capture_includes_paused_sources(tmp_path):
    from utils.monitor_capture import capture_urls
    limits = RetryLimits(tmp_path)
    for _ in range(3):
        limits.failed("processing", URL, "page_not_accessible")
    config = {"monitors": {"daily": {"targets": [{"urls": [URL]}]}}}
    from utils.monitor_frequency import SCHEDULE_FILE, record_check
    started = datetime.now(timezone.utc)
    atomic_json(tmp_path / SCHEDULE_FILE, {URL: record_check(None, at=started, ok=True, changed=False)})
    assert capture_urls(tmp_path, config, set(), retry_only=False, capture_all=False, started=started) == {URL}
