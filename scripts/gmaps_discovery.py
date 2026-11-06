#!/usr/bin/env python3
"""Discover businesses for a city through the google-maps-scraper web API.

Writes a JSON list of places that ``scripts/process_discovery_results.py`` consumes. Read-only
against the catalogue. The scraper runs on the Firecrawl server (127.0.0.1:8081 there, task
``CostFinderGmapsScraper``); open the tunnel first with
``ssh -f -N -L 127.0.0.1:18081:127.0.0.1:8081 firecrawl``. The address comes from the environment:
``GMAPS_SCRAPER_URL`` (default http://127.0.0.1:18081, the tunnel) and optional
``GMAPS_SCRAPER_PROXIES`` (comma-separated proxy URLs; the server reaches Google directly, so none is needed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.gmaps_scraper_client import DEFAULT_KEYWORD_TEMPLATES, GmapsClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True)
    parser.add_argument("--state", required=True, help="two-letter state code")
    parser.add_argument("--keyword", action="append", help="repeatable; {city} and {state} are substituted")
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--max-time", type=int, default=900)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    base_url = os.getenv("GMAPS_SCRAPER_URL", "http://127.0.0.1:18081")
    proxies = [p.strip() for p in os.getenv("GMAPS_SCRAPER_PROXIES", "").split(",") if p.strip()]
    templates = args.keyword or list(DEFAULT_KEYWORD_TEMPLATES)
    keywords = [t.format(city=args.city, state=args.state) for t in templates]

    client = GmapsClient(base_url, proxies=proxies)
    places = client.discover(
        f"costfinder-{args.city}-{args.state}", keywords, depth=args.depth, max_time=args.max_time,
        wait_timeout=args.max_time + 600,
    )
    args.out.write_text(json.dumps(places, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"keywords={keywords} places={len(places)} wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
