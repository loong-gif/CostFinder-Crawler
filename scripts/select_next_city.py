#!/usr/bin/env python3
"""Manage the new-city queue in ``state/crawl_schedule/cities.json``.

  add CITY STATE [--supply N]   add a candidate city
  pick                          choose the next city (weekly slot) and mark it onboarding
  pilot CITY STATE              record the pilot result; the qualifying count (places that passed the filter and
                                have a website) comes from candidates.json written by register-businesses

Demand (signups per city) is read with read-sql + read-snapshot, or from ``--snapshot``.
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

from utils.city_selection import evaluate_pilot, pick_next_city, pilot_qualifying, score_city  # noqa: E402
from utils.crawl_schedule_state import city_key, load_state, save_state  # noqa: E402
from utils.pipeline_db import fresh_snapshot, load_snapshot  # noqa: E402

DEMAND_TABLES = {"profiles": ("profiles", "id, location_city", "location_city is not null")}
OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "select_city"


def load_demand(snapshot: Path | None, now: datetime) -> dict[str, int]:
    """Signups per lower-cased city."""
    path = snapshot or fresh_snapshot(OUTPUT_DIR / now.strftime("%Y%m%dT%H%M%SZ"), DEMAND_TABLES)
    rows = load_snapshot(path, DEMAND_TABLES)["profiles"]
    return dict(Counter(str(r["location_city"]).strip().lower() for r in rows if str(r["location_city"]).strip()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("city")
    add.add_argument("state")
    add.add_argument("--supply", type=float, default=0)
    pick = sub.add_parser("pick")
    pick.add_argument("--snapshot", type=Path, help="snapshot.json of the demand query; default: read it now")
    pilot = sub.add_parser("pilot")
    pilot.add_argument("city")
    pilot.add_argument("state")
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
        demand = load_demand(args.snapshot, now)
        key = pick_next_city(cities, demand, now)
        if key is None:
            print("no eligible candidate (or onboarding slots are full)")
            return 0
        cities[key]["status"] = "onboarding"
        save_state("cities", cities)
        print(f"next city: {key} score={score_city(cities[key], cities, demand)}; marked onboarding")
        return 0

    key = city_key(args.city, args.state)
    if key not in cities:
        print(f"unknown city {key}", file=sys.stderr)
        return 2
    qualifying = pilot_qualifying(load_state("candidates"), args.city)
    result = evaluate_pilot(cities[key], qualifying, now)
    print(f"{key}: qualifying={qualifying} {cities[key].get('status')} -> {result['status']} "
          f"parked_until={result['parked_until']}")
    cities[key] = result
    save_state("cities", cities)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
