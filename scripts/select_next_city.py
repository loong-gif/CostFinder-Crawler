#!/usr/bin/env python3
"""Manage the new-city queue in ``state/crawl_schedule/cities.json``.

  add CITY STATE [--supply N]   add a candidate city
  pick [--apply]                choose the next city (weekly slot) and mark it onboarding
  pilot CITY STATE N [--apply]  record the pilot result (N qualifying businesses with a website)
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.city_selection import evaluate_pilot, pick_next_city, score_city  # noqa: E402
from utils.crawl_schedule_state import city_key, load_state, save_state  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402


def load_demand() -> dict[str, int]:
    """Signups per lower-cased city (read-only); unreachable demand data counts as zero."""
    try:
        client = load_supabase_client(PROJECT_ROOT, role="service")
        rows = fetch_all_rows(client, "profiles", "id,location_city", order="id.asc")
    except Exception as exc:  # demand is advisory, never a blocker
        print(f"demand unavailable: {exc}", file=sys.stderr)
        return {}
    return dict(Counter(str(r.get("location_city") or "").strip().lower() for r in rows if r.get("location_city")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("city")
    add.add_argument("state")
    add.add_argument("--supply", type=float, default=0)
    pick = sub.add_parser("pick")
    pick.add_argument("--apply", action="store_true")
    pilot = sub.add_parser("pilot")
    pilot.add_argument("city")
    pilot.add_argument("state")
    pilot.add_argument("qualifying", type=int)
    pilot.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cities = load_state("cities")

    if args.command == "add":
        key = city_key(args.city, args.state)
        if key in cities:
            print(f"{key} already exists with status {cities[key].get('status')}")
            return 0
        cities[key] = {
            "city": args.city, "state": args.state, "status": "candidate", "expected_supply": args.supply,
            "business_count": 0, "discovery_interval_days": 30, "last_discovery_at": None,
            "next_discovery_at": None, "parked_until": None, "failed_pilots": 0,
        }
        save_state("cities", cities)
        print(f"added {key}")
        return 0

    if args.command == "pick":
        demand = load_demand()
        key = pick_next_city(cities, demand, now)
        if key is None:
            print("no eligible candidate (or onboarding slots are full)")
            return 0
        print(f"next city: {key} score={score_city(cities[key], cities, demand)}")
        if args.apply:
            cities[key]["status"] = "onboarding"
            save_state("cities", cities)
            print("marked onboarding")
        return 0

    key = city_key(args.city, args.state)
    if key not in cities:
        print(f"unknown city {key}", file=sys.stderr)
        return 2
    result = evaluate_pilot(cities[key], args.qualifying, now)
    print(f"{key}: {cities[key].get('status')} -> {result['status']} parked_until={result['parked_until']}")
    if args.apply:
        cities[key] = result
        save_state("cities", cities)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
