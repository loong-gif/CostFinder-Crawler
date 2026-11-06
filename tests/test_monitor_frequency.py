from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from utils.monitor_frequency import SCHEDULE_FILE, due_urls, record_check, seed_schedule
from utils.monitor_price_store import atomic_json

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def days(n: float) -> datetime:
    return T0 + timedelta(days=n)


def history(*change_days: float, checked: float) -> dict:
    entry = record_check(None, at=T0, ok=True, changed=False)
    for day in change_days:
        entry = record_check(entry, at=days(day), ok=True, changed=True)
    return record_check(entry, at=days(checked), ok=True, changed=False)


def test_new_page_is_polled_weekly_and_slows_down_while_it_stays_unchanged():
    assert history(checked=14)["tier_days"] == 7
    assert history(checked=28)["tier_days"] == 15
    assert history(checked=70)["tier_days"] == 30


def test_tier_follows_how_often_the_price_lines_change():
    weekly = history(7, 14, 21, 28, checked=30)
    monthly = history(30, 60, 90, checked=95)
    rare = history(100, 220, checked=225)
    assert (weekly["tier_days"], monthly["tier_days"], rare["tier_days"]) == (7, 15, 30)
    assert monthly["next_due_at"] == days(95 + 15).isoformat()


def test_a_page_that_stops_changing_moves_to_a_slower_tier():
    assert history(7, 14, 21, checked=50)["tier_days"] == 15
    assert history(7, 14, 21, checked=90)["tier_days"] == 30


def test_failed_capture_waits_one_tier_and_records_no_change():
    entry = history(30, 60, checked=62)
    failed = record_check(entry, at=days(77), ok=False, changed=False)
    assert failed["change_times"] == entry["change_times"]
    assert failed["last_checked_at"] == entry["last_checked_at"]
    assert failed["next_due_at"] == days(77 + 15).isoformat()


def test_only_due_or_untracked_pages_are_selected():
    schedule = {"https://a.test/specials": history(checked=1), "https://b.test/specials": history(checked=6)}
    selected = due_urls(schedule, ["https://a.test/specials", "https://b.test/specials", "https://c.test/new"], days(9))
    assert selected == {"https://a.test/specials", "https://c.test/new"}


def test_seed_rebuilds_frequency_from_existing_captures_and_price_change_events():
    captures = [("https://a.test/specials", days(n).isoformat()) for n in range(0, 120, 7)]
    changes = [("https://a.test/specials", days(n).isoformat()) for n in (30, 61, 91)]
    schedule = seed_schedule(["https://a.test/specials", "https://never.test"], captures=captures,
                             changes=changes, now=days(119))
    entry = schedule["https://a.test/specials"]
    assert (entry["tier_days"], entry["first_seen_at"], entry["last_checked_at"]) == (15, T0.isoformat(),
                                                                                       days(119).isoformat())
    assert "https://never.test" not in schedule


def test_daily_monitor_run_captures_only_due_pages_and_records_the_result(tmp_path, monkeypatch):
    import scripts.run_monitor_price_sync as sync

    due, fresh = "https://due.test/specials", "https://fresh.test/specials"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema_version": 1, "monitors": {"daily": {"targets": [{"urls": [due, fresh]}]}}}))
    now = datetime.now(timezone.utc)
    atomic_json(tmp_path / SCHEDULE_FILE, {
        due: record_check(None, at=now - timedelta(days=8), ok=True, changed=False),
        fresh: record_check(None, at=now - timedelta(days=2), ok=True, changed=False)})
    captured = []

    def run_monitor(config, name, *, urls_filter, **kwargs):
        captured.append(sorted(urls_filter))
        return {"monitor_name": name, "error": 0, "successful_urls": sorted(urls_filter),
                "changed_urls": sorted(urls_filter), "page_errors": []}

    monkeypatch.setattr(sync, "run_monitor", run_monitor)
    monkeypatch.setattr(sync, "database_snapshot", lambda: {"promotions": [], "confirmations": [], "audit": []})
    monkeypatch.setattr(sync, "enqueue_expired_campaign_checks", lambda *a: 0)
    monkeypatch.setattr("sys.argv", ["sync", "--state-dir", str(tmp_path), "--config", str(config)])
    sync.main()
    assert captured == [[due]]
    schedule = json.loads((tmp_path / SCHEDULE_FILE).read_text())
    assert len(schedule[due]["change_times"]) == 1 and schedule[fresh]["change_times"] == []


def test_membership_pages_are_polled_every_60_days_and_slow_to_90_when_they_stay_unchanged():
    young = record_check(None, at=T0, ok=True, changed=False, kind="membership")
    assert young["tier_days"] == 60 and young["next_due_at"] == days(60).isoformat()
    unchanged = record_check(young, at=days(120), ok=True, changed=False, kind="membership")
    assert unchanged["tier_days"] == 90
    frequent = record_check(None, at=T0, ok=True, changed=False, kind="membership")
    for day in (30, 60, 90):
        frequent = record_check(frequent, at=days(day), ok=True, changed=True, kind="membership")
    assert frequent["tier_days"] == 60


def test_the_same_history_is_a_faster_tier_for_a_promotion_page_than_for_a_membership_page():
    promotion = record_check(None, at=T0, ok=True, changed=False)
    promotion = record_check(promotion, at=days(70), ok=True, changed=False)
    membership = record_check(None, at=T0, ok=True, changed=False, kind="membership")
    membership = record_check(membership, at=days(70), ok=True, changed=False, kind="membership")
    assert (promotion["tier_days"], membership["tier_days"]) == (30, 60)
