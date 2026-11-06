"""Send the Feishu run summary after a monitor run (called by the server launch scripts).

The report is ``<state-dir>/sync_report.json`` when it was generated after ``--started-at`` and the monitor
exited with 0 or 2; otherwise the run is reported as terminated, with the tail of ``--log``.
Hourly retries that captured no page and processed no event send nothing.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.firecrawl_self_hosted_monitor import load_config  # noqa: E402
from utils.monitor_frequency import SCHEDULE_FILE, tier_summary  # noqa: E402
from utils.monitor_price_store import read_json  # noqa: E402
from utils.monitor_queue import build_config, config_urls  # noqa: E402
from utils.run_notification import crash_message, report_message, run_has_work, send_text  # noqa: E402

LOG_TAIL_LINES = 15


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="monitor config; default: the queue of the state's database_snapshot.json")
    parser.add_argument("--run-type", required=True, choices=["full_monitor", "failure_retry"])
    parser.add_argument("--started-at", required=True, help="ISO time the monitor run started")
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="print the message instead of sending it")
    args = parser.parse_args()
    report = read_json(args.state_dir / "sync_report.json", {})
    started = datetime.fromisoformat(args.started_at)
    fresh = (args.exit_code in (0, 2) and report.get("run_type") == args.run_type
             and datetime.fromisoformat(report["generated_at"]) >= started)
    if fresh:
        if args.run_type == "failure_retry" and not run_has_work(report):
            print("idle retry run: no notification")
            return 0
        config = (load_config(args.config) if args.config
                  else build_config(read_json(args.state_dir / "database_snapshot.json"), args.state_dir))
        urls = config_urls(config)
        schedule = read_json(args.state_dir / SCHEDULE_FILE)
        tiers = tier_summary(schedule, urls, datetime.now(timezone.utc)) if schedule is not None else None
        text = report_message(report, started_at=args.started_at, exit_code=args.exit_code, tiers=tiers)
    else:
        tail = args.log.read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:]
        text = crash_message(run_type=args.run_type, started_at=args.started_at, exit_code=args.exit_code,
                             log_tail=tail)
    if args.dry_run:
        print(text)
        return 0
    print(f"sent {send_text(text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
