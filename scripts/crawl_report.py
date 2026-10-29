#!/usr/bin/env python3
"""Summarise crawl scheduling state: cities, tiers, due counts and failures (read-only)."""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.crawl_schedule_state import load_state  # noqa: E402


def main() -> int:
    now = datetime.now(timezone.utc)
    schedule = load_state("schedule")
    cities = load_state("cities")
    candidates = load_state("candidates")

    print("cities:", dict(Counter(c.get("status") for c in cities.values())))
    due_cities = [
        key for key, c in cities.items()
        if c.get("status") == "active" and c.get("next_discovery_at")
        and datetime.fromisoformat(c["next_discovery_at"]) <= now
    ]
    print(f"cities due for re-discovery: {len(due_cities)}")
    print("tiers:", dict(Counter(e.get("tier") for e in schedule.values())))
    due = [e for e in schedule.values() if e.get("next_crawl_at") and datetime.fromisoformat(e["next_crawl_at"]) <= now]
    print(f"businesses due now: {len(due)} by tier {dict(Counter(e.get('tier') for e in due))}")
    failing = {k: e for k, e in schedule.items() if int(e.get("consecutive_failures") or 0) > 0}
    print(f"businesses with failures: {len(failing)} (tier D: {sum(1 for e in schedule.values() if e.get('tier') == 'D')})")
    print(f"discovery candidates: {len(candidates)} (passing: {sum(1 for c in candidates.values() if c.get('passed_filter'))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
