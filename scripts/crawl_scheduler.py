#!/usr/bin/env python3
"""Plan and record scheduled re-crawls from ``state/crawl_schedule`` (no database writes).

``plan``   reads the catalogue with read-sql + read-snapshot (``--management-api`` on the monitor host, or ``--snapshot``), adds schedule and city entries
           for businesses registered since the last run, lists businesses due now (bounded by ``--cap``) and cities
           due for re-discovery; with ``--apply`` writes the state files, ``due.json`` and monitor-style buckets.
``record`` applies crawl results (JSON list of ``{business_id, ok, changed}``) to the schedule.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.city_selection import due_for_rediscovery  # noqa: E402
from utils.crawl_schedule_seed import SEED_TABLES, seed  # noqa: E402
from utils.crawl_schedule_state import load_state, save_state, schedule_lock  # noqa: E402
from utils.crawl_scheduler_logic import (  # noqa: E402
    apply_result,
    build_buckets,
    collect_business_urls,
    select_due,
)
from utils.firecrawl_scrape_raw_db import canonical_scrape_url  # noqa: E402
from utils.monitor_database_snapshot import api_snapshot  # noqa: E402
from utils.pipeline_db import fresh_snapshot, load_snapshot  # noqa: E402

DEFAULT_CAP = 150
OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "crawl_schedule"


def monitor_urls(snapshot: dict) -> set[str]:
    """The monitor queue: source URLs of clinic_promotions and clinic_memberships."""
    found = {canonical_scrape_url(str(row.get("source_url") or ""))
             for table in ("promotions", "memberships") for row in snapshot[table]}
    found.discard("")
    return found


def monitor_config(buckets: list[list[str]], now: datetime) -> dict:
    """Monitor-style config for the due URLs; a candidate file, never merged into the static config."""
    stamp = now.strftime("%Y%m%d")
    monitors = {}
    for index, urls in enumerate(buckets, start=1):
        name = f"costfinder_due_{stamp}_{index:02d}"
        monitors[name] = {
            "name": name,
            "status": "active",
            "targets": [{"id": f"{name}_scrape", "type": "scrape", "urls": urls}],
        }
    return {"schema_version": 1, "generated_at": now.isoformat(), "monitors": monitors}


def cmd_plan(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    read = api_snapshot if args.management_api else fresh_snapshot
    path = args.snapshot or read(OUTPUT_DIR / now.strftime("%Y%m%dT%H%M%SZ"), SEED_TABLES)
    snapshot = load_snapshot(path, SEED_TABLES)
    schedule, cities = seed(snapshot, load_state("schedule"), load_state("cities"), now)
    added = len(schedule) - len(load_state("schedule"))
    urls_by_business = collect_business_urls(snapshot["businesses"], snapshot["promotions"],
                                             snapshot["services"], snapshot["memberships"])
    due = select_due(schedule, now, args.cap, urls_by_business)
    due_urls = [url for item in due for url in item["urls"]]
    covered = set(due_urls) & monitor_urls(snapshot)
    print(f"seeded_businesses={added} due_businesses={len(due)} due_urls={len(set(due_urls))} "
          f"already_in_monitor={len(covered)}")
    by_tier: dict[str, int] = {}
    for item in due:
        by_tier[item["tier"]] = by_tier.get(item["tier"], 0) + 1
    print(f"by_tier={by_tier} cap={args.cap}")
    print(f"cities due for re-discovery: {len(due_for_rediscovery(cities, now))} (discover-businesses --due)")
    if args.apply:
        save_state("schedule", schedule)
        save_state("cities", cities)
        buckets = build_buckets(due_urls)
        save_state("due", {"generated_at": now.isoformat(), "businesses": due, "buckets": buckets})
        save_state("due_monitor", monitor_config(buckets, now))
        print(f"wrote schedule.json, cities.json, due.json and due_monitor.json with {len(buckets)} bucket(s)")
    else:
        print("dry run: pass --apply to write the state files")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    schedule = load_state("schedule")
    updated = 0
    for result in results:
        key = str(int(result["business_id"]))
        if key not in schedule:
            continue
        schedule[key] = apply_result(
            schedule[key],
            business_id=int(key),
            ok=bool(result.get("ok")),
            changed=bool(result.get("changed")),
            now=now,
            expiring_campaign=bool(result.get("expiring_campaign")),
        )
        updated += 1
    print(f"results={len(results)} applied={updated}")
    if args.apply:
        print(f"wrote {save_state('schedule', schedule)}")
    else:
        print("dry run: pass --apply to write schedule.json")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--cap", type=int, default=DEFAULT_CAP)
    plan.add_argument("--snapshot", type=Path, help="snapshot.json of the read-only query; default: read it now")
    plan.add_argument("--management-api", action="store_true",
                      help="monitor host: run read.sql through the Supabase Management API (no Supabase CLI there)")
    plan.add_argument("--apply", action="store_true")
    plan.set_defaults(func=cmd_plan)
    record = sub.add_parser("record")
    record.add_argument("--results", required=True)
    record.add_argument("--apply", action="store_true")
    record.set_defaults(func=cmd_record)
    args = parser.parse_args()
    with schedule_lock():
        return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
