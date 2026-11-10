#!/usr/bin/env python3
"""Discover new promotion / membership pages with the self-hosted Firecrawl Map endpoint.

For each business website: map the site, keep same-site URLs whose path suggests a promotion,
membership or price page and that are not already stored, scrape them, and keep only pages that state a
real discount or price visible on the page. Results go to ``<batch-dir>/discovered.json``:

  - businesses that failed or met a down search engine are rerun in the same run until stable;
  - pages whose scrape failed are retried (backoff, then a bounded public HTTP fetch) before writing;
  - running the command again on the same batch skips businesses already finished there.

The database is read through an automatic read-only snapshot (read.sql + Supabase CLI); nothing is written.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from utils.discovery_batch import DISCOVERY_DIR, discovered_path  # noqa: E402
from utils.discovery_retry import retry_failed_pages, retry_one  # noqa: E402
from utils.discovery_search import CloudSearch, SearchHealth  # noqa: E402
from utils.discovery_snapshot import business_contexts, business_rows, known_urls, read_discovery_snapshot  # noqa: E402
from utils.service_endpoints import FIRECRAWL_URL  # noqa: E402
from utils.page_discovery_run import (  # noqa: E402
    Firecrawl,
    discover_business,
    discover_per_site,
    discover_until_stable,
    finished,
    merge_results,
    select_discovery_businesses,
)


def summarize(results: List[Dict[str, Any]], args: argparse.Namespace, retried: Dict[str, int], elapsed: float
              ) -> Dict[str, Any]:
    pages = [p for r in results for p in r["pages"]]
    return {
        "businesses": len(results), "errors": sum(1 for r in results if r["error"]),
        "unfinished": sum(1 for r in results if not finished(r)),
        "mapped_urls": sum(r["mapped"] for r in results), "candidates": sum(r["candidates"] for r in results),
        "from_search": sum(1 for p in pages if "search" in (p.get("sources") or [])),
        "search_ok": sum(1 for r in results if r.get("search_status") == "ok"),
        "search_no_results": sum(1 for r in results if r.get("search_status") == "no_results"),
        "search_skipped_engines_down": sum(1 for r in results if r.get("search_status") == "skipped_engines_down"),
        "search_skipped_budget": sum(1 for r in results if r.get("search_status") == "skipped_budget"),
        "cloud_credits_spent": getattr(args.search_client, "spent", 0),
        "cloud_searches": getattr(args.search_client, "searches", 0),
        "hub_pages": sum(r.get("hubs", 0) for r in results),
        "hop_pages": sum(1 for p in pages if p.get("hop") == 1),
        "hop_valid": sum(1 for p in pages if p.get("hop") == 1 and p["status"] in ("valid", "needs_review")),
        "valid": sum(1 for p in pages if p["status"] == "valid"),
        "needs_review": sum(1 for p in pages if p["status"] == "needs_review"),
        "rejected": sum(1 for p in pages if p["status"] == "rejected"),
        "scrape_failed": sum(1 for p in pages if p["status"] == "scrape_failed"),
        "retried_urls": retried,
        "elapsed_s": round(elapsed),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-dir", type=Path, default=DISCOVERY_DIR)
    parser.add_argument("--business-id", type=int, action="append")
    parser.add_argument("--city")
    parser.add_argument("--limit", type=int, default=20, help="max businesses to process")
    parser.add_argument("--after-business-id", type=int, default=0)
    parser.add_argument("--map-limit", type=int, default=500)
    parser.add_argument("--max-candidates", type=int, default=8)
    parser.add_argument("--allow-blog", action="store_true")
    parser.add_argument("--sources", default="map,search",
                        help="comma-separated: map (/v2/map), search (site-restricted /v1/search)")
    parser.add_argument("--search-limit", type=int, default=10)
    parser.add_argument("--search-backend", choices=("self", "cloud"), default="self",
                        help="cloud = api.firecrawl.dev /v2/search (2 credits per search); candidates are still "
                             "scraped on the self-hosted instance")
    parser.add_argument("--credit-budget", type=int, default=300, help="cloud: max credits this run may spend")
    parser.add_argument("--credit-reserve", type=int, default=150, help="cloud: credits that must remain afterwards")
    parser.add_argument("--uncovered-from", type=Path,
                        help="earlier discovery output: only businesses without a valid page there; "
                             "never-processed businesses first")
    parser.add_argument("--search-interval", type=float, default=4.0,
                        help="minimum seconds between search requests (SearXNG upstream engines throttle bursts)")
    parser.add_argument("--search-retry-delay", type=float, default=15.0, help="pause before retrying an empty search")
    parser.add_argument("--hops", type=int, default=1, choices=(0, 1),
                        help="1 = follow offers one click from hub pages that state no price themselves")
    parser.add_argument("--max-hop-links", type=int, default=8, help="links followed per hub page")
    parser.add_argument("--max-pages", type=int, default=24, help="page budget per business")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seeds", type=Path,
                        help="topic-search seeds.json: candidate pages per site; without --business-id only these sites run")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    fc = Firecrawl(FIRECRAWL_URL,os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY", ""), search_interval=args.search_interval)
    args.search_health = SearchHealth()
    args.search_client = None
    if args.search_backend == "cloud":
        keys = re.findall(r"^FIRECRAWL_API_KEY=\s*['\"]?(fc-[A-Za-z0-9]+)", (PROJECT_ROOT / ".env").read_text(), re.M)
        if not keys:
            raise SystemExit("no FIRECRAWL_API_KEY in .env")
        # .env holds two keys; the first is the account that still has credits (checked below)
        client_cloud = CloudSearch(keys[0], budget=args.credit_budget, reserve=args.credit_reserve)
        remaining = client_cloud.remaining_credits()
        client_cloud.budget = client_cloud.effective_budget()
        print(f"cloud search: {remaining} credits remaining, budget this run {client_cloud.budget} "
              f"(~{client_cloud.budget // CloudSearch.COST} searches), reserve {args.credit_reserve}", flush=True)
        args.search_client = client_cloud

    snapshot = read_discovery_snapshot(args.batch_dir)
    rows = [r for r in business_rows(snapshot) if r["business_id"] > args.after_business_id]
    seeds = ({root: site["candidates"] for root, site in json.loads(args.seeds.read_text()).items()}
             if args.seeds is not None else None)
    rows = select_discovery_businesses(rows, set(args.business_id or []), seeds)
    args.seeds = seeds or {}
    if args.city:
        rows = [r for r in rows if (r.get("city") or "").lower() == args.city.lower()]
    if args.uncovered_from:
        covered = {b["business_id"] for b in json.loads(args.uncovered_from.read_text())["businesses"]
                   if any(p.get("status") in ("valid", "needs_review") for p in b.get("pages", []))}
        context = business_contexts(snapshot)
        rows = [r for r in rows if r["business_id"] not in covered]
        rows.sort(key=lambda r: (not (context.get(r["business_id"]) and context[r["business_id"]].never_processed),
                                 r["business_id"]))
    out = discovered_path(args.batch_dir)
    earlier = json.loads(out.read_text())["businesses"] if out.exists() else []
    done = {r["business_id"] for r in earlier if finished(r)}
    rows = [r for r in rows if r["business_id"] not in done][: args.limit]
    known = known_urls(snapshot)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        def run_round(group: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return discover_per_site(group, lambda b: discover_business(fc, b, known, args), pool.map)

        def reset_search() -> None:
            args.search_health = SearchHealth()

        fresh = discover_until_stable(rows, run_round, reset=reset_search)
        results = merge_results(earlier, fresh)
        retried = retry_failed_pages(results, lambda url, record: retry_one(fc, url, record), workers=args.workers)

    summary = summarize(results, args, retried, time.monotonic() - started)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "summary": summary,
                               "businesses": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
