"""Discover promotion pages by topic search in a city (self-hosted Firecrawl /v1/search).

Runs every --query template for every --area, keeps hits whose site belongs to a
Google Maps place (from discover-businesses) in one of the --city values, and
writes to --out-dir:
  hits.json             every search hit (query, url, title, description)
  register_places.json  matched places: the --input of register-businesses
  seeds.json            per site: website and promotion-like hit pages, for discover-pages --seeds
  report.json           search precision (clinic hit rate, out-of-area and unmatched sites)
Read-only against the database.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.discover_new_promo_pages import Firecrawl  # noqa: E402
from utils.topic_search import (DEFAULT_QUERY_TEMPLATES, expand_queries, match_hits, run_topic_search,  # noqa: E402
                                search_report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--area", action="append", required=True, help="search area, repeatable ({area} in queries)")
    parser.add_argument("--city", action="append", required=True,
                        help="Google Maps city a matched place must be in, repeatable")
    parser.add_argument("--places", action="append", type=Path, required=True,
                        help="discover-businesses output, repeatable")
    parser.add_argument("--query", action="append", help=f"query template, repeatable (default {DEFAULT_QUERY_TEMPLATES})")
    parser.add_argument("--hits", type=Path, help="reuse an earlier hits.json instead of searching")
    parser.add_argument("--limit", type=int, default=10, help="results per query")
    parser.add_argument("--interval", type=float, default=4.0, help="minimum seconds between queries")
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()

    out_dir = args.out_dir or PROJECT_ROOT / "output/pipeline/topic_search" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.hits:
        hits = json.loads(args.hits.read_text())
    else:
        load_dotenv(PROJECT_ROOT / ".env")
        firecrawl = Firecrawl(os.environ["FIRECRAWL_SELF_HOSTED_API_URL"].strip("'\""),
                              os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY", ""))
        queries = expand_queries(args.query or DEFAULT_QUERY_TEMPLATES, args.area)
        hits = run_topic_search(firecrawl.search, queries, limit=args.limit, interval=args.interval)
    places = [place for path in args.places for place in json.loads(path.read_text())]
    matched = match_hits(hits, places, args.city)
    report = search_report(hits, matched)
    register = list({p["google_place_id"]: p for c in matched["clinics"] for p in c["places"]}.values())
    seeds = {c["root"]: {"website": c["website"], "candidates": c["candidates"]} for c in matched["clinics"]}
    (out_dir / "hits.json").write_text(json.dumps(hits, indent=1, ensure_ascii=False))
    (out_dir / "register_places.json").write_text(json.dumps(register, indent=1, ensure_ascii=False))
    (out_dir / "seeds.json").write_text(json.dumps(seeds, indent=1, ensure_ascii=False))
    (out_dir / "report.json").write_text(json.dumps({**report, "matched": matched}, indent=1, ensure_ascii=False))
    print(json.dumps({**report, "out_dir": str(out_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
