#!/usr/bin/env python3
"""Apply external discovery output (JSON list of places) to the catalogue and crawl state.

Input items: google_place_id, name, address, city, review_count, score, category, website.
Dry run by default. ``--apply`` renders ``output/pipeline/register_businesses/<UTC timestamp>/apply.sql``
(one DO block that re-checks duplicates and matches, then inserts/links/refreshes ``master_business_info``
rows; run it through the Supabase MCP) and updates candidate/city state. Schedule entries for newly
created businesses come from ``seed_crawl_schedule.py --apply`` after the SQL has run.
Existing businesses are never deleted or demoted.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.crawl_schedule_state import city_key, load_state, save_state  # noqa: E402
from utils.discovery_logic import classify_places  # noqa: E402
from utils.pipeline_db import write_text  # noqa: E402
from utils.register_businesses import render_register  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "register_businesses"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="JSON file with discovered places")
    parser.add_argument("--city", help="city name this run covered (updates its discovery timestamps)")
    parser.add_argument("--state", default="", help="state code for --city")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--actions", default="link,refresh",
        help="comma-separated actions to write with --apply: link, refresh, insert, promote "
             "(default link,refresh: backfill ids and metrics, never create businesses)",
    )
    args = parser.parse_args()
    wanted = {a.strip() for a in args.actions.split(",") if a.strip()}

    places = json.loads(Path(args.input).read_text(encoding="utf-8"))
    client = load_supabase_client(PROJECT_ROOT, role="service")
    master = fetch_all_rows(
        client, "master_business_info", "business_id,name,city,website,google_place_id,review_count,score",
        order="business_id.asc",
    )
    master_by_place = {row["google_place_id"]: row for row in master if row.get("google_place_id")}

    now = datetime.now(timezone.utc)
    candidates, actions = classify_places(
        places, master_by_place, load_state("candidates"), now, unlinked_masters=master
    )
    print({name: len(items) for name, items in actions.items()})
    if not args.apply:
        print("dry run: pass --apply to write")
        return 0

    run_dir = OUTPUT_DIR / now.strftime("%Y%m%dT%H%M%SZ")
    apply_path = write_text(run_dir / "apply.sql", render_register(actions, wanted))
    cities = load_state("cities")
    if args.city:
        key = city_key(args.city, args.state)
        entry = cities.get(key)
        if entry:
            entry["last_discovery_at"] = now.isoformat()
            entry["next_discovery_at"] = (now + timedelta(days=int(entry["discovery_interval_days"]))).isoformat()
            cities[key] = entry
    save_state("candidates", candidates)
    save_state("cities", cities)
    print(apply_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
