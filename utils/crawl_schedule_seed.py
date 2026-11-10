"""抓取调度补种：目录里还没有调度条目的商家和城市，按已有数据生成 schedule.json / cities.json 条目（已有条目不动）。"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.crawl_schedule_state import TIER_INTERVAL_DAYS, city_key, discovery_interval_days

_STATE_ZIP = re.compile(r",\s*([A-Z]{2})\s+\d{5}")

SEED_TABLES = {
    "businesses": ("master_business_info", "business_id, website, city, address", "true"),
    "promotions": ("clinic_promotions", "promotion_id, business_id, source_url, updated_at, is_active", "true"),
    "services": ("clinic_services", "service_id, business_id, source_url, updated_at", "true"),
    "memberships": ("clinic_memberships", "plan_id, business_id, source_url, updated_at", "true"),
}


def parse_state(address: str | None) -> str:
    match = _STATE_ZIP.search(address or "")
    return match.group(1) if match else ""


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def latest_by_business(rows: list[dict[str, Any]]) -> dict[int, datetime]:
    latest: dict[int, datetime] = {}
    for row in rows:
        stamp = _parse_ts(row.get("updated_at"))
        business_id = row.get("business_id")
        if stamp is None or business_id is None:
            continue
        key = int(business_id)
        if key not in latest or stamp > latest[key]:
            latest[key] = stamp
    return latest


def build_schedule(
    businesses: list[dict[str, Any]],
    *,
    last_crawled: dict[int, datetime],
    tier_a_ids: set[int],
    now: datetime,
    existing: dict[str, Any],
) -> dict[str, Any]:
    """Add one entry per business not yet scheduled; overdue entries are spread over their interval."""
    schedule = dict(existing)
    ordered = sorted(int(row["business_id"]) for row in businesses)
    for position, business_id in enumerate(ordered):
        key = str(business_id)
        if key in schedule:
            continue
        tier = "A" if business_id in tier_a_ids else "B"
        interval = TIER_INTERVAL_DAYS[tier]
        last = last_crawled.get(business_id)
        due = (last + timedelta(days=interval)) if last else now
        if due <= now:
            due = now + timedelta(days=position % interval)
        schedule[key] = {
            "tier": tier,
            "interval_days": interval,
            "last_crawled_at": last.isoformat() if last else None,
            "next_crawl_at": due.isoformat(),
            "last_status": None,
            "consecutive_failures": 0,
            "consecutive_no_change": 0,
            "last_change_at": None,
            "pinned": False,
        }
    return schedule


def build_cities(businesses: list[dict[str, Any]], existing: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One entry per (city, state). A row without a state in its address inherits the state of the
    same city name when that name maps to exactly one known state; otherwise the state stays blank."""
    pairs = [
        (str(row.get("city") or "").strip(), parse_state(row.get("address")))
        for row in businesses
        if str(row.get("city") or "").strip()
    ]
    known: dict[str, set[str]] = {}
    for city, state in pairs:
        if state:
            known.setdefault(city, set()).add(state)
    counts: Counter = Counter()
    for city, state in pairs:
        if not state and len(known.get(city, ())) == 1:
            state = next(iter(known[city]))
        counts[(city, state)] += 1

    cities = dict(existing)
    for (city, state), count in counts.items():
        key = city_key(city, state)
        if key in cities:
            continue
        cities[key] = {
            "city": city,
            "state": state,
            "status": "active",
            "business_count": count,
            "discovery_interval_days": discovery_interval_days(count),
            "last_discovery_at": None,
            "next_discovery_at": now.isoformat(),
            "parked_until": None,
        }
    return cities


def seed(snapshot: dict[str, list[dict[str, Any]]], schedule: dict[str, Any], cities: dict[str, Any],
         now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """新登记的商家与城市补进调度；返回 (schedule, cities)。"""
    promotions, services, memberships = snapshot["promotions"], snapshot["services"], snapshot["memberships"]
    tier_a = {int(r["business_id"]) for r in promotions if r.get("is_active") and r.get("business_id")}
    tier_a |= {int(r["business_id"]) for r in memberships if r.get("business_id")}
    return (build_schedule(snapshot["businesses"], last_crawled=latest_by_business(promotions + services + memberships),
                           tier_a_ids=tier_a, now=now, existing=schedule),
            build_cities(snapshot["businesses"], cities, now))
