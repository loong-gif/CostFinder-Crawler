from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config.discovery_filters import passes_filter
from utils import crawl_schedule_seed as seed
from utils.crawl_schedule_state import discovery_interval_days, load_state, save_state

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def test_parse_state_from_address() -> None:
    assert seed.parse_state("31655 Coast Hwy Suite A, Laguna Beach, CA 92651") == "CA"
    assert seed.parse_state(None) == ""


def test_build_schedule_tiers_spread_and_preserve_existing() -> None:
    businesses = [{"business_id": 1}, {"business_id": 2}, {"business_id": 3}]
    recent = NOW - timedelta(days=5)
    existing = {"3": {"tier": "C", "pinned": True}}
    schedule = seed.build_schedule(
        businesses,
        last_crawled={1: recent},
        tier_a_ids={1},
        now=NOW,
        existing=existing,
    )
    assert schedule["1"]["tier"] == "A" and schedule["1"]["interval_days"] == 15
    assert schedule["1"]["next_crawl_at"] == (recent + timedelta(days=15)).isoformat()
    assert schedule["2"]["tier"] == "B" and schedule["2"]["last_crawled_at"] is None
    assert datetime.fromisoformat(schedule["2"]["next_crawl_at"]) >= NOW
    assert schedule["3"] == {"tier": "C", "pinned": True}


def test_build_cities_intervals() -> None:
    rows = [{"business_id": i, "city": "Big", "address": "x, Big, CA 90000"} for i in range(25)]
    rows.append({"business_id": 99, "city": "Tiny", "address": "x, Tiny, CA 90001"})
    rows.append({"business_id": 100, "city": None, "address": None})
    cities = seed.build_cities(rows, {}, NOW)
    assert cities["Big|CA"]["discovery_interval_days"] == 14
    assert cities["Tiny|CA"]["discovery_interval_days"] == 60
    assert len(cities) == 2 and discovery_interval_days(10) == 30


def test_state_roundtrip_and_filter(tmp_path) -> None:
    save_state("schedule", {"1": {"tier": "B"}}, tmp_path)
    assert load_state("schedule", tmp_path) == {"1": {"tier": "B"}}
    assert passes_filter({"review_count": 50, "score": 4.0, "website": "https://a.com"})
    assert not passes_filter({"review_count": 49, "score": 4.8, "website": "https://a.com"})
    assert not passes_filter({"review_count": 80, "score": 4.8, "website": ""})
    assert not passes_filter({"review_count": 80, "score": 3.9, "website": "https://a.com"})
    assert not passes_filter({"review_count": 80, "score": None, "website": "https://a.com"})


def test_build_cities_inherits_unambiguous_state() -> None:
    rows = [
        {"business_id": 1, "city": "Irvine", "address": "x, Irvine, CA 92618"},
        {"business_id": 2, "city": "Irvine", "address": "22 Odyssey Ste 155"},
        {"business_id": 3, "city": "Lake Forest", "address": "x, Lake Forest, CA 92630"},
        {"business_id": 4, "city": "Lake Forest", "address": "x, Lake Forest, IL 60045"},
        {"business_id": 5, "city": "Lake Forest", "address": "23600 Rockfield Blvd Suite 2N"},
    ]
    cities = seed.build_cities(rows, {}, NOW)
    assert cities["Irvine|CA"]["business_count"] == 2
    assert {"Lake Forest|CA", "Lake Forest|IL", "Lake Forest|"} == {k for k in cities if k.startswith("Lake")}
