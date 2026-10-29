#!/usr/bin/env python3
"""Plan and record scheduled re-crawls from ``state/crawl_schedule`` (no database writes).

``plan``   lists businesses due now (bounded by ``--cap``) and compares their URLs with the
           static monitor config; with ``--apply`` writes ``due.json`` and monitor-style buckets.
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

from utils.crawl_schedule_state import load_state, save_state  # noqa: E402
from utils.crawl_scheduler_logic import (  # noqa: E402
    apply_result,
    build_buckets,
    collect_business_urls,
    select_due,
)
from utils.firecrawl_scrape_raw_db import canonical_scrape_url  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402

MONITOR_CONFIG = PROJECT_ROOT / "config" / "firecrawl_monitor_migration.json"
DEFAULT_CAP = 150


def monitor_urls() -> set[str]:
    config = json.loads(MONITOR_CONFIG.read_text(encoding="utf-8"))
    found: set[str] = set()
    for monitor in config.get("monitors", {}).values():
        for target in monitor.get("targets", []):
            found.update(canonical_scrape_url(url) for url in target.get("urls", []))
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


def load_business_urls() -> dict[int, set[str]]:
    client = load_supabase_client(PROJECT_ROOT, role="service")
    businesses = fetch_all_rows(client, "master_business_info", "business_id,website", order="business_id.asc")
    promotions = fetch_all_rows(client, "clinic_promotions", "promotion_id,business_id,source_url", order="promotion_id.asc")
    services = fetch_all_rows(client, "clinic_services", "service_id,business_id,source_url", order="service_id.asc")
    memberships = fetch_all_rows(client, "clinic_memberships", "plan_id,business_id,source_url", order="plan_id.asc")
    return collect_business_urls(businesses, promotions, services, memberships)


def cmd_plan(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    schedule = load_state("schedule")
    if not schedule:
        print("schedule.json is empty; run scripts/seed_crawl_schedule.py --apply first", file=sys.stderr)
        return 2
    urls_by_business = load_business_urls()
    due = select_due(schedule, now, args.cap, urls_by_business)
    due_urls = [url for item in due for url in item["urls"]]
    covered = set(due_urls) & monitor_urls()
    print(f"due_businesses={len(due)} due_urls={len(set(due_urls))} already_in_monitor={len(covered)}")
    by_tier: dict[str, int] = {}
    for item in due:
        by_tier[item["tier"]] = by_tier.get(item["tier"], 0) + 1
    print(f"by_tier={by_tier} cap={args.cap}")
    if args.apply:
        buckets = build_buckets(due_urls)
        save_state("due", {"generated_at": now.isoformat(), "businesses": due, "buckets": buckets})
        save_state("due_monitor", monitor_config(buckets, now))
        print(f"wrote due.json and due_monitor.json with {len(buckets)} bucket(s)")
    else:
        print("dry run: pass --apply to write due.json")
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
    plan.add_argument("--apply", action="store_true")
    plan.set_defaults(func=cmd_plan)
    record = sub.add_parser("record")
    record.add_argument("--results", required=True)
    record.add_argument("--apply", action="store_true")
    record.set_defaults(func=cmd_record)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
