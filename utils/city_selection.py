"""New-city selection and pilot gate rules."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

MAX_ONBOARDING = 2
PILOT_MIN_QUALIFYING = 15
PARK_DAYS = 90


def score_city(entry: Mapping[str, Any], cities: Mapping[str, Mapping[str, Any]], demand: Mapping[str, int]) -> float:
    """Adjacency (active cities in the same state) and demand weigh most; failed pilots penalise."""
    state = str(entry.get("state") or "")
    adjacent = sum(
        1 for other in cities.values()
        if other.get("status") == "active" and state and other.get("state") == state
    )
    signups = int(demand.get(str(entry.get("city") or "").strip().lower(), 0))
    supply = float(entry.get("expected_supply") or 0)
    failed = int(entry.get("failed_pilots") or 0)
    return round(3 * min(adjacent, 5) + 2 * signups + supply / 10 - 5 * failed, 2)


def pick_next_city(
    cities: Mapping[str, Mapping[str, Any]], demand: Mapping[str, int], now: datetime
) -> Optional[str]:
    """Best eligible candidate key, or None when the onboarding slots are full."""
    if sum(1 for c in cities.values() if c.get("status") == "onboarding") >= MAX_ONBOARDING:
        return None
    ranked: List[tuple[float, str]] = []
    for key, entry in cities.items():
        if entry.get("status") != "candidate":
            continue
        parked_until = entry.get("parked_until")
        if parked_until and datetime.fromisoformat(parked_until) > now:
            continue
        ranked.append((score_city(entry, cities, demand), key))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1]


def due_for_rediscovery(cities: Mapping[str, Mapping[str, Any]], now: datetime) -> List[str]:
    """到期需要重新发现的 active 城市，从来没发现过的排最前，其余按到期时间先后。"""
    due = [key for key, entry in cities.items() if entry.get("status") == "active"
           and (not entry.get("next_discovery_at") or datetime.fromisoformat(entry["next_discovery_at"]) <= now)]
    return sorted(due, key=lambda key: (cities[key].get("last_discovery_at") is not None,
                                        cities[key].get("next_discovery_at") or "", key))


def pilot_qualifying(candidates: Mapping[str, Mapping[str, Any]], city: str) -> int:
    """试点城市里通过过滤线且有网站的地点数（来自 register-businesses 写入的 candidates.json）。"""
    name = city.strip().casefold()
    return sum(1 for entry in candidates.values()
               if str(entry.get("city") or "").strip().casefold() == name
               and entry.get("passed_filter") and str(entry.get("website") or "").strip())


def evaluate_pilot(entry: Mapping[str, Any], qualifying_count: int, now: datetime) -> Dict[str, Any]:
    """Return the entry after a pilot: active when enough businesses qualify, else parked for 90 days."""
    new = dict(entry)
    if qualifying_count >= PILOT_MIN_QUALIFYING:
        new.update(status="active", parked_until=None)
    else:
        new.update(
            status="candidate",
            parked_until=(now + timedelta(days=PARK_DAYS)).isoformat(),
            failed_pilots=int(entry.get("failed_pilots") or 0) + 1,
        )
    return new
