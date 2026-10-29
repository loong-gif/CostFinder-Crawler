from __future__ import annotations

from datetime import datetime, timedelta, timezone

from utils.crawl_scheduler_logic import (
    apply_result,
    build_buckets,
    collect_business_urls,
    jitter_days,
    select_due,
)

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def _entry(tier="B", **extra):
    base = {"tier": tier, "interval_days": 30, "next_crawl_at": NOW.isoformat(),
            "consecutive_failures": 0, "consecutive_no_change": 0, "pinned": False}
    base.update(extra)
    return base


def test_collect_urls_uses_website_only_as_fallback() -> None:
    urls = collect_business_urls(
        [{"business_id": 1, "website": "https://A.com/"}, {"business_id": 2, "website": "https://b.com"}],
        [{"business_id": 1, "source_url": "https://a.com/specials/"}],
    )
    assert urls[1] == {"https://a.com/specials"} and urls[2] == {"https://b.com/"}


def test_select_due_orders_by_tier_skips_future_and_respects_cap() -> None:
    schedule = {
        "1": _entry("B"),
        "2": _entry("A"),
        "3": _entry("A", next_crawl_at=(NOW + timedelta(days=1)).isoformat()),
        "4": _entry("C"),
    }
    urls = {1: {"u1"}, 2: {"u2"}, 3: {"u3"}, 4: {"u4"}}
    due = select_due(schedule, NOW, 2, urls)
    assert [item["business_id"] for item in due] == [2, 1]
    assert all("_sort" not in item for item in due)


def test_failures_back_off_to_tier_d() -> None:
    entry = _entry(consecutive_failures=2)
    result = apply_result(entry, business_id=1, ok=False, changed=False, now=NOW)
    assert result["tier"] == "D" and result["interval_days"] == 30
    again = apply_result(result, business_id=1, ok=False, changed=False, now=NOW)
    assert again["interval_days"] == 60
    recovered = apply_result(again, business_id=1, ok=True, changed=False, now=NOW)
    assert recovered["tier"] == "B" and recovered["consecutive_failures"] == 0


def test_change_promotes_and_no_change_demotes() -> None:
    up = apply_result(_entry("B"), business_id=1, ok=True, changed=True, now=NOW)
    assert up["tier"] == "A" and up["interval_days"] == 15 and up["last_change_at"]
    entry = _entry("A", consecutive_no_change=2)
    down = apply_result(entry, business_id=1, ok=True, changed=False, now=NOW)
    assert down["tier"] == "B" and down["consecutive_no_change"] == 0
    held = apply_result(entry, business_id=1, ok=True, changed=False, now=NOW, expiring_campaign=True)
    assert held["tier"] == "A"
    pinned = apply_result(_entry("A", consecutive_no_change=2, pinned=True), business_id=1, ok=True, changed=False, now=NOW)
    assert pinned["tier"] == "A"


def test_next_crawl_uses_interval_and_jitter() -> None:
    result = apply_result(_entry("B"), business_id=5, ok=True, changed=False, now=NOW)
    expected = NOW + timedelta(days=30 + jitter_days(5))
    assert result["next_crawl_at"] == expected.isoformat()
    assert -3 <= jitter_days(12345) <= 3


def test_buckets_dedupe_and_split() -> None:
    buckets = build_buckets([f"u{i % 120}" for i in range(300)], size=50)
    assert [len(b) for b in buckets] == [50, 50, 20]


def test_monitor_config_shape() -> None:
    from scripts.crawl_scheduler import monitor_config

    config = monitor_config([["u1", "u2"], ["u3"]], NOW)
    assert list(config["monitors"]) == ["costfinder_due_20261015_01", "costfinder_due_20261015_02"]
    first = config["monitors"]["costfinder_due_20261015_01"]
    assert first["targets"][0]["urls"] == ["u1", "u2"] and first["targets"][0]["type"] == "scrape"
