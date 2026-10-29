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
