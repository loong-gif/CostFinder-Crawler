"""Discover promotion pages by topic search in a city (self-hosted Firecrawl /v1/search).

Runs every --query template for every --area, keeps hits whose site belongs to a Google Maps place in one
of the --city values, and writes to --out-dir:
  hits.json             every search hit (query, url, title, description)
  lookups.json          Google Maps places found per unmatched site ("<site> <lookup city>, <state>")
  register_places.json  matched places: the --input of register-businesses
  seeds.json            per site: website and promotion-like hit pages, for discover-pages --seeds
  report.json           search precision (clinic hit rate, out-of-area and unmatched sites)
Sites without a matching place are looked up in Google Maps and matched again until no site is left
unlooked-up. Running again with the same --out-dir reuses hits.json and lookups.json, so finished searches
and lookups are not repeated. Read-only against the database.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.gmaps_scraper_client import GMAPS_SCRAPER_URL, GmapsClient  # noqa: E402
from utils.service_endpoints import FIRECRAWL_URL  # noqa: E402
from utils.page_discovery_run import Firecrawl  # noqa: E402
from utils.topic_search import (DEFAULT_QUERY_TEMPLATES, expand_queries, lookup_keyword,  # noqa: E402
                                match_with_lookups, run_topic_search, search_report)

LOOKUP_WAIT_S = 150  # 单关键词补查任务：scraper 偶尔抓完不结束任务，不等满 max_time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--area", action="append", required=True, help="search area, repeatable ({area} in queries)")
    parser.add_argument("--city", action="append", required=True,
                        help="Google Maps city a matched place must be in, repeatable")
    parser.add_argument("--state", required=True, help="two-letter state code for Google Maps lookups")
    parser.add_argument("--lookup-city", help="city in lookup keywords (default: the first --city)")
    parser.add_argument("--places", action="append", type=Path, required=True,
                        help="discover-businesses output, repeatable")
    parser.add_argument("--query", action="append", help=f"query template, repeatable (default {DEFAULT_QUERY_TEMPLATES})")
    parser.add_argument("--limit", type=int, default=10, help="results per query")
    parser.add_argument("--interval", type=float, default=4.0, help="minimum seconds between queries")
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()

    out_dir = args.out_dir or PROJECT_ROOT / "output/pipeline/topic_search" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    hits_path, lookups_path = out_dir / "hits.json", out_dir / "lookups.json"
    if hits_path.exists():
        hits = json.loads(hits_path.read_text())
    else:
        load_dotenv(PROJECT_ROOT / ".env")
        firecrawl = Firecrawl(FIRECRAWL_URL, os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY", ""))
        queries = expand_queries(args.query or DEFAULT_QUERY_TEMPLATES, args.area)
        hits = run_topic_search(firecrawl.search, queries, limit=args.limit, interval=args.interval)
        hits_path.write_text(json.dumps(hits, indent=1, ensure_ascii=False))
    places = [place for path in args.places for place in json.loads(path.read_text())]
    done = json.loads(lookups_path.read_text()) if lookups_path.exists() else {}
    proxies = [p.strip() for p in os.getenv("GMAPS_SCRAPER_PROXIES", "").split(",") if p.strip()]
    city = args.lookup_city or args.city[0]

    client = GmapsClient(GMAPS_SCRAPER_URL, proxies=proxies)

    def lookup(root: str) -> List[Dict[str, Any]]:
        return client.discover(f"costfinder-lookup-{root}", [lookup_keyword(root, city, args.state)],
                               depth=1, max_time=900, wait_timeout=LOOKUP_WAIT_S)

    def save(lookups: Dict[str, List[Dict[str, Any]]]) -> None:
        lookups_path.write_text(json.dumps(lookups, indent=1, ensure_ascii=False))

    matched, _, lookups = match_with_lookups(hits, places, args.city, lookup, done=done, save=save)
    report = search_report(hits, matched)
    register = list({p["google_place_id"]: p for c in matched["clinics"] for p in c["places"]}.values())
    seeds = {c["root"]: {"website": c["website"], "candidates": c["candidates"]} for c in matched["clinics"]}
    (out_dir / "register_places.json").write_text(json.dumps(register, indent=1, ensure_ascii=False))
    (out_dir / "seeds.json").write_text(json.dumps(seeds, indent=1, ensure_ascii=False))
    (out_dir / "report.json").write_text(json.dumps({**report, "lookups": len(lookups), "matched": matched},
                                                    indent=1, ensure_ascii=False))
    print(json.dumps({**report, "lookups": len(lookups), "out_dir": str(out_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
