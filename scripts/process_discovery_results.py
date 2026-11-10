#!/usr/bin/env python3
"""Register discovered places (JSON list) in the catalogue and crawl state.

Input items: google_place_id, name, address, city, review_count, score, category, website.
Existing businesses are read with read-sql + read-snapshot (or ``--snapshot``). The command classifies every
place (insert / promote / link / refresh by the confirmed filter line) and renders
``output/pipeline/register_businesses/<UTC timestamp>/apply.sql``: one DO block whose statements skip places
that are already registered, so re-running the same places never registers them twice. Run it through the
Supabase MCP; the next ``crawl-schedule plan`` adds the new businesses to the crawl schedule. Places whose
website matches several existing businesses that the street address cannot separate are listed in
``ambiguous.json`` and re-evaluated on the next discovery. Existing businesses are never deleted or demoted.
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

from utils.discovery_register import read_master, register_places  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "register_businesses"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="JSON file with discovered places")
    parser.add_argument("--city", help="city name this run covered (updates its discovery timestamps)")
    parser.add_argument("--state", default="", help="state code for --city")
    parser.add_argument("--snapshot", type=Path, help="snapshot.json of master_business_info; default: read it now")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    run_dir = OUTPUT_DIR / now.strftime("%Y%m%dT%H%M%SZ")
    places = json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(register_places(places, read_master(run_dir, args.snapshot), city=args.city, state=args.state,
                          now=now, run_dir=run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
