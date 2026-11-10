"""Show the monitor poll schedule: pages per 7 / 15 / 30 day (promotion) and 60 / 90 day (membership) tier and pages due now (read only).

Without ``poll_schedule.json`` the schedule is rebuilt in memory from the state's evidence and events,
exactly as the first daily monitor run will build and save it.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.firecrawl_self_hosted_monitor import load_config  # noqa: E402
from utils.monitor_frequency import SCHEDULE_FILE, seed_from_state, tier_summary, update_gap_days  # noqa: E402
from utils.monitor_price_store import read_json  # noqa: E402
from utils.monitor_queue import build_config, config_kinds, config_urls  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "output/self_hosted_monitor_state")
    parser.add_argument("--config", type=Path, help="monitor config; default: the queue of the state's database_snapshot.json")
    parser.add_argument("--pages", action="store_true", help="also list every page with its tier and next due time")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    config = (load_config(args.config) if args.config
              else build_config(read_json(args.state_dir / "database_snapshot.json"), args.state_dir))
    urls = sorted(set(config_urls(config)))
    path = args.state_dir / SCHEDULE_FILE
    schedule = read_json(path) if path.exists() else seed_from_state(args.state_dir, urls, now, config_kinds(config))
    print(json.dumps({"source": str(path) if path.exists() else "rebuilt_from_state",
                      **tier_summary(schedule, urls, now)}, ensure_ascii=False))
    if args.pages:
        for url in urls:
            entry = schedule.get(url)
            if entry is None:
                print(f"untracked\t-\t-\t{url}")
                continue
            print(f"{entry['tier_days']}d\t{update_gap_days(entry, now):.0f}d_gap\t"
                  f"{entry['next_due_at'][:10]}\t{len(entry['change_times'])}_changes\t{url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
