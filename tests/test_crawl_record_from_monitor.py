from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from scripts.crawl_record_from_monitor import load_monitor_run, results_from_monitor

SINCE = datetime(2026, 10, 15, tzinfo=timezone.utc)


def test_results_ok_failed_and_changed() -> None:
    due = [
        {"business_id": 1, "urls": ["https://a.com/specials/"]},
        {"business_id": 2, "urls": ["https://b.com/x", "https://b.com/y"]},
        {"business_id": 3, "urls": ["https://c.com/z"]},
    ]
    pages = {"https://a.com/specials": {"last_status": "same"}, "https://b.com/y": {"last_status": "changed"}}
    results = results_from_monitor(
        due, captured_pages=pages, event_urls=set(), failed_urls={"https://c.com/z"}
    )
    assert results == [
        {"business_id": 1, "ok": True, "changed": False},
        {"business_id": 2, "ok": True, "changed": True},
        {"business_id": 3, "ok": False, "changed": False},
    ]


def test_unattempted_business_is_skipped_not_failed() -> None:
    due = [{"business_id": 9, "urls": ["https://never-run.com/p"]}]
    assert results_from_monitor(due, captured_pages={}, event_urls=set(), failed_urls=set()) == []


def test_event_marks_change_even_when_status_same() -> None:
    due = [{"business_id": 1, "urls": ["https://a.com/p"]}]
    results = results_from_monitor(
        due, captured_pages={"https://a.com/p": {"last_status": "same"}}, event_urls={"https://a.com/p"}
    )
    assert results[0]["changed"] is True


def test_load_monitor_run_ignores_stale_snapshots_and_events(tmp_path) -> None:
    (tmp_path / "events").mkdir()
    fresh = {"updated_at": "2026-10-16T00:00:00+00:00", "pages": {"https://a.com/p": {"last_status": "changed"}}}
    stale = {"updated_at": "2026-10-01T00:00:00+00:00", "pages": {"https://b.com/p": {"last_status": "changed"}}}
    (tmp_path / "m1.json").write_text(json.dumps(fresh))
    (tmp_path / "m2.json").write_text(json.dumps(stale))
    event = tmp_path / "events" / "e1.json"
    event.write_text(json.dumps({"status": "content_changed", "url": "https://a.com/p"}))
    old_event = tmp_path / "events" / "e2.json"
    old_event.write_text(json.dumps({"status": "content_changed", "url": "https://b.com/p"}))
    os.utime(old_event, (0, 0))
    (tmp_path / "failed_evidence").mkdir()
    (tmp_path / "failed_evidence" / "f1.json").write_text(
        json.dumps({"url": "https://f.com/p", "captured_at": "2026-10-16T00:00:00+00:00"})
    )
    (tmp_path / "failed_evidence" / "f2.json").write_text(
        json.dumps({"url": "https://old.com/p", "captured_at": "2026-10-01T00:00:00+00:00"})
    )
    pages, events, failed = load_monitor_run(tmp_path, ["m1", "m2", "missing"], SINCE)
    assert set(pages) == {"https://a.com/p"} and events == {"https://a.com/p"}
    assert failed == {"https://f.com/p"}
