from __future__ import annotations

from datetime import datetime, timedelta, timezone

from utils.city_selection import MAX_ONBOARDING, evaluate_pilot, pick_next_city, score_city

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def _cities():
    return {
        "Irvine|CA": {"city": "Irvine", "state": "CA", "status": "active"},
        "Tustin|CA": {"city": "Tustin", "state": "CA", "status": "active"},
        "Anaheim|CA": {"city": "Anaheim", "state": "CA", "status": "candidate"},
        "Reno|NV": {"city": "Reno", "state": "NV", "status": "candidate"},
    }


def test_adjacency_and_demand_drive_pick() -> None:
    cities = _cities()
    assert pick_next_city(cities, {}, NOW) == "Anaheim|CA"
    assert pick_next_city(cities, {"reno": 5}, NOW) == "Reno|NV"
    assert score_city(cities["Reno|NV"], cities, {"reno": 5}) == 10.0


def test_onboarding_slots_and_parked_cities_block_pick() -> None:
    cities = _cities()
    cities["Anaheim|CA"]["parked_until"] = (NOW + timedelta(days=5)).isoformat()
    assert pick_next_city(cities, {}, NOW) == "Reno|NV"
    for index in range(MAX_ONBOARDING):
        cities[f"X{index}|CA"] = {"city": f"X{index}", "state": "CA", "status": "onboarding"}
    assert pick_next_city(cities, {}, NOW) is None


def test_pilot_gate() -> None:
    ok = evaluate_pilot({"status": "onboarding"}, 15, NOW)
    assert ok["status"] == "active"
    failed = evaluate_pilot({"status": "onboarding", "failed_pilots": 1}, 14, NOW)
    assert failed["status"] == "candidate" and failed["failed_pilots"] == 2
    assert datetime.fromisoformat(failed["parked_until"]) == NOW + timedelta(days=90)
