#!/usr/bin/env python3
"""Seed ``state/crawl_schedule`` files from existing catalogue data (read-only against the DB).

Dry run by default; ``--apply`` writes ``cities.json`` and ``schedule.json``. Existing entries
are preserved (a re-run only adds new businesses and cities).
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.crawl_schedule_state import (  # noqa: E402
    TIER_INTERVAL_DAYS,
    city_key,
    discovery_interval_days,
    load_state,
    save_state,
)
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402

_STATE_ZIP = re.compile(r",\s*([A-Z]{2})\s+\d{5}")


def parse_state(address: str | None) -> str:
    match = _STATE_ZIP.search(address or "")
    return match.group(1) if match else ""


def _parse_ts(value: Any) -> datetime | None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write state files (default: dry run)")
    args = parser.parse_args()

    client = load_supabase_client(PROJECT_ROOT, role="service")
    businesses = fetch_all_rows(
        client, "master_business_info", "business_id,city,address", order="business_id.asc"
    )
    promotions = fetch_all_rows(
        client, "clinic_promotions", "promotion_id,business_id,updated_at,is_active", order="promotion_id.asc"
    )
    services = fetch_all_rows(
        client, "clinic_services", "service_id,business_id,updated_at", order="service_id.asc"
    )
    memberships = fetch_all_rows(
        client, "clinic_memberships", "plan_id,business_id,updated_at", order="plan_id.asc"
    )

    last_crawled = latest_by_business(promotions + services + memberships)
    tier_a_ids = {int(row["business_id"]) for row in promotions if row.get("is_active") and row.get("business_id")}
    tier_a_ids |= {int(row["business_id"]) for row in memberships if row.get("business_id")}

    now = datetime.now(timezone.utc)
    schedule = build_schedule(
        businesses, last_crawled=last_crawled, tier_a_ids=tier_a_ids, now=now, existing=load_state("schedule")
    )
    cities = build_cities(businesses, load_state("cities"), now)

    tiers = Counter(entry["tier"] for entry in schedule.values())
    print(f"businesses={len(businesses)} schedule={len(schedule)} tiers={dict(tiers)} cities={len(cities)}")
    print(f"with_last_crawled={sum(1 for e in schedule.values() if e['last_crawled_at'])}")
    if args.apply:
        print(f"wrote {save_state('schedule', schedule)}")
        print(f"wrote {save_state('cities', cities)}")
    else:
        print("dry run: pass --apply to write state files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
