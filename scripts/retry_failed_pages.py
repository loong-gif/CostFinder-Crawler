#!/usr/bin/env python3
"""Retry pages whose scrape failed in earlier discovery runs.

Collects every ``scrape_failed`` page from the given discovery outputs (skipping URLs that later
succeeded), retries them at low concurrency with backoff on the self-hosted Firecrawl, and falls back
to a bounded public HTTP fetch. Output has the same shape as the discovery files so the review step can
read it. Read-only against the database; uses no cloud credits.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from scripts.discover_new_promo_pages import Firecrawl  # noqa: E402
from utils.direct_public_scrape import DEFAULT_HEADERS, html_to_text  # noqa: E402
from utils.promo_page_discovery import assess_offer_page, extract_end_dates  # noqa: E402
from utils.url_safety import assert_safe_crawl_entry_url  # noqa: E402


def collect_failed(paths: Iterable[Path]) -> Dict[str, Dict[str, Any]]:
    """Unique failed URLs (with every business that failed on them and the original sources), minus URLs that
    later succeeded. 连锁分店共用同一网站时同一网址只重试一次，结果写给每家分店。"""
    failed: Dict[str, Dict[str, Any]] = {}
    good: set = set()
    for path in paths:
        for business in json.loads(path.read_text(encoding="utf-8")).get("businesses", []):
            for page in business.get("pages", []):
                url = page["url"]
                if page.get("status") == "scrape_failed":
                    record = failed.setdefault(url, {
                        "businesses": [], "kind_hint": page.get("kind_hint") or "promotion", "sources": [],
                        "title": page.get("title", ""), "hop": page.get("hop", 0), "via": page.get("via"),
                        "error": page.get("error"),
                    })
                    if business["business_id"] not in {b["business_id"] for b in record["businesses"]}:
                        record["businesses"].append({key: business.get(key) for key in
                                                     ("business_id", "name", "city", "website")})
                    for source in page.get("sources") or []:
                        if source not in record["sources"]:
                            record["sources"].append(source)
                else:
                    good.add(url)
    return {u: r for u, r in failed.items() if u not in good}


def direct_fetch(url: str, timeout: float = 20) -> Dict[str, str]:
    """Bounded public fetch: no access controls bypassed, SSRF-guarded, HTML documents only."""
    safe = assert_safe_crawl_entry_url(url)
    response = requests.get(safe, headers=DEFAULT_HEADERS, timeout=timeout)
    response.raise_for_status()
    if "html" not in response.headers.get("Content-Type", "").casefold():
        raise ValueError("unsupported_content_type")
    html = response.text
    return {"markdown": html_to_text(html), "rawHtml": html}


def retry_one(
    fc: Any, url: str, record: Dict[str, Any], *, attempts: int = 3, backoff: float = 5.0,
    fetch: Callable[[str], Dict[str, str]] = direct_fetch, sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    page: Optional[Dict[str, str]] = None
    engine, last_error = "", ""
    for attempt in range(attempts):
        try:
            page, engine = fc.scrape(url), "firecrawl"
            if page["markdown"].strip() or page["rawHtml"].strip():
                break
            page, last_error = None, "empty_page"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        if attempt < attempts - 1:
            sleep(backoff * (3 ** attempt))
    if page is None:
        try:
            page, engine = fetch(url), "direct_http"
        except Exception as exc:
            return {"url": url, "hop": record["hop"], "via": record["via"], "status": "scrape_failed",
                    "sources": record["sources"], "title": record["title"],
                    "error": f"{last_error}; direct: {type(exc).__name__}: {str(exc)[:100]}", "retried": True}
    assessment = assess_offer_page(page["markdown"], page["rawHtml"], kind_hint=record["kind_hint"])
    return {
        "url": url, "hop": record["hop"], "via": record["via"], "kind_hint": record["kind_hint"],
        "kind": assessment.kind, "sources": record["sources"], "title": record["title"],
        "end_dates": extract_end_dates(page["markdown"]), "retried": True, "engine": engine,
        "status": ("needs_review" if assessment.needs_review else "valid") if assessment.valid else "rejected",
        "evidence": list(assessment.evidence), "reasons": list(assessment.reasons),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    api_url = os.getenv("FIRECRAWL_SELF_HOSTED_API_URL", "http://127.0.0.1:3002").strip("'\"")
    fc = Firecrawl(api_url, os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY", ""), timeout=150)
    failed = collect_failed(args.input)
    items = list(failed.items())[: args.limit]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        pages = list(pool.map(lambda kv: (kv[1], retry_one(fc, kv[0], kv[1], attempts=args.attempts)), items))

    businesses: Dict[int, Dict[str, Any]] = {}
    for record, page in pages:
        for business in record["businesses"]:
            entry = businesses.setdefault(business["business_id"], {**business, "pages": []})
            entry["pages"].append(page)
    counts = {s: sum(1 for _, p in pages if p["status"] == s)
              for s in ("valid", "needs_review", "rejected", "scrape_failed")}
    summary = {"urls": len(items), **counts,
               "recovered_by_firecrawl": sum(1 for _, p in pages if p.get("engine") == "firecrawl"),
               "recovered_by_direct_http": sum(1 for _, p in pages if p.get("engine") == "direct_http"),
               "elapsed_s": round(time.monotonic() - started)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "summary": summary,
                                    "businesses": list(businesses.values())}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
