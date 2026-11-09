#!/usr/bin/env python3
"""Discover businesses through the google-maps-scraper web API.

``--city C --state S --out FILE`` writes a JSON list of places for one city (``register-businesses`` input).
``--due`` re-discovers every active city whose ``next_discovery_at`` has passed (``state/crawl_schedule/cities.json``,
never-discovered cities first, at most ``--max-cities``): each city's places go to
``output/pipeline/discover_businesses/<UTC timestamp>/<city>_<state>.json`` and are registered right away
(apply.sql per city, run through the Supabase MCP), which also moves the city's next discovery date.

The scraper runs on the Firecrawl server (127.0.0.1:8081 there, task ``CostFinderGmapsScraper``); when it is
unreachable the command opens an SSH tunnel to it for the run and closes it afterwards
(``utils.gmaps_scraper_client.gmaps_tunnel``). The address comes from the environment: ``GMAPS_SCRAPER_URL``
(default http://127.0.0.1:18081, the tunnel) and optional ``GMAPS_SCRAPER_PROXIES`` (comma-separated proxy URLs;
the server reaches Google directly, so none is needed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.city_selection import due_for_rediscovery  # noqa: E402
from utils.crawl_schedule_state import load_state  # noqa: E402
from utils.discovery_register import read_master, register_places  # noqa: E402
from utils.gmaps_scraper_client import (  # noqa: E402
    DEFAULT_KEYWORD_TEMPLATES,
    GMAPS_SCRAPER_URL,
    GmapsClient,
    gmaps_tunnel,
)

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "discover_businesses"


def scraper_client() -> GmapsClient:
    proxies = [p.strip() for p in os.getenv("GMAPS_SCRAPER_PROXIES", "").split(",") if p.strip()]
    return GmapsClient(GMAPS_SCRAPER_URL, proxies=proxies)


def discover_city(client: GmapsClient, city: str, state: str, args: argparse.Namespace) -> list[dict]:
    keywords = [t.format(city=city, state=state) for t in (args.keyword or DEFAULT_KEYWORD_TEMPLATES)]
    places = client.discover(f"costfinder-{city}-{state}", keywords, depth=args.depth, max_time=args.max_time,
                             wait_timeout=args.wait_timeout or args.max_time + 600)
    print(f"{city}|{state}: keywords={keywords} places={len(places)}")
    return places


def discover_due(client: GmapsClient, args: argparse.Namespace) -> None:
    now = datetime.now(timezone.utc)
    run_dir = OUTPUT_DIR / now.strftime("%Y%m%dT%H%M%SZ")
    cities = load_state("cities")
    due = due_for_rediscovery(cities, now)[:args.max_cities]
    print(f"cities due for re-discovery: {len(due)} this run")
    master = read_master(run_dir) if due else []
    for key in due:
        city, state = cities[key]["city"], cities[key]["state"]
        places = discover_city(client, city, state, args)
        slug = f"{city}_{state}".replace(" ", "_")
        (run_dir / f"{slug}.json").write_text(json.dumps(places, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(register_places(places, master, city=city, state=state, now=now, run_dir=run_dir / slug))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city")
    parser.add_argument("--state", help="two-letter state code")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--due", action="store_true", help="re-discover and register every city that is due")
    parser.add_argument("--max-cities", type=int, default=5)
    parser.add_argument("--keyword", action="append", help="repeatable; {city} and {state} are substituted")
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--max-time", type=int, default=900)
    parser.add_argument("--wait-timeout", type=int,
                        help="seconds to wait for the job (default max-time + 600); the scraper sometimes never "
                             "finishes a job after scraping it, so short single-keyword lookups use a small value")
    args = parser.parse_args()
    if not args.due and not (args.city and args.state and args.out):
        parser.error("--city, --state and --out are required without --due")
    client = scraper_client()
    with gmaps_tunnel():
        if args.due:
            discover_due(client, args)
            return 0
        places = discover_city(client, args.city, args.state, args)
    args.out.write_text(json.dumps(places, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
