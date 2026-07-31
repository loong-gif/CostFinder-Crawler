#!/usr/bin/env python3
"""Discover and persist clinic special/promo pages with Firecrawl Map + Scrape."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.clinic_promotions_db import upsert_promotion
from utils.firecrawl_client import get_firecrawl_client, load_firecrawl_env, scrape_page_markdown
from utils.firecrawl_scrape_raw_db import canonical_scrape_url, save_scrape_response, scrape_request_fingerprint
from utils.scrape_markdown import prepare_scrape_markdown
from utils.supabase_access import fetch_all_rows, load_supabase_client

PROMOTION_PATH_RE = re.compile(r"(?:special|promo)", re.IGNORECASE)


def _normalized_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def map_links_to_urls(links: Iterable[Any]) -> list[str]:
    """Normalize SDK v1/v2 Map link variants to their URL strings."""
    urls: list[str] = []
    for link in links:
        if isinstance(link, str):
            url = link
        elif isinstance(link, dict):
            url = str(link.get("url") or "")
        else:
            url = str(getattr(link, "url", "") or "")
        if url:
            urls.append(url)
    return urls


def discover_promotion_urls(website: str, mapped_urls: Iterable[str]) -> list[str]:
    """Keep canonical same-site URLs whose path contains special or promo."""
    root = canonical_scrape_url(website)
    root_host = _normalized_host(root)
    if not root_host:
        raise ValueError(f"invalid website: {website!r}")

    seen: set[str] = set()
    matches: list[str] = []
    for candidate in mapped_urls:
        canonical = canonical_scrape_url(candidate)
        parsed = urlparse(canonical)
        if not canonical or _normalized_host(canonical) != root_host:
            continue
        if not PROMOTION_PATH_RE.search(parsed.path or ""):
            continue
        if canonical not in seen:
            seen.add(canonical)
            matches.append(canonical)
    return matches


def map_site(client: Any, website: str, *, limit: int, timeout_ms: int) -> list[str]:
    """Use Firecrawl's current v2 Map endpoint and normalize its link output."""
    api_url = str(getattr(client, "api_url", "") or "").rstrip("/")
    if api_url and "api.firecrawl.dev" not in api_url.lower():
        response = _legacy_request(
            api_url,
            "/v1/map",
            {"url": canonical_scrape_url(website), "limit": limit},
            timeout_secs=max(90, timeout_ms / 1000 + 30),
        )
        if not response.get("success"):
            raise RuntimeError(str(response.get("error") or response.get("details") or "map failed"))
        return map_links_to_urls(response.get("links") or [])
    result = client.map(
        canonical_scrape_url(website),
        include_subdomains=False,
        ignore_query_parameters=True,
        limit=limit,
        sitemap="include",
        timeout=timeout_ms,
    )
    return map_links_to_urls(getattr(result, "links", []) or [])


def _legacy_request(api_url: str, path: str, payload: dict[str, Any], *, timeout_secs: float) -> dict[str, Any]:
    load_firecrawl_env()
    api_key = str(os.getenv("FIRECRAWL_API_KEY") or "").strip()
    response = requests.post(
        f"{api_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout_secs,
    )
    try:
        body = response.json()
    except ValueError as exc:
        response.raise_for_status()
        raise RuntimeError("self-hosted Firecrawl returned non-JSON") from exc
    if not response.ok:
        raise RuntimeError(str(body.get("error") or body.get("details") or f"HTTP {response.status_code}"))
    return body


def scrape_and_capture(client: Any, source_url: str) -> tuple[str, dict[str, Any]]:
    """Scrape with v2 cloud SDK or the self-hosted server's v1 endpoint."""
    api_url = str(getattr(client, "api_url", "") or "").rstrip("/")
    if not api_url or "api.firecrawl.dev" in api_url.lower():
        return scrape_page_markdown(client, source_url)
    body = _legacy_request(
        api_url,
        "/v1/scrape",
        {"url": source_url, "formats": ["markdown", "links"]},
        timeout_secs=150,
    )
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return prepare_scrape_markdown(str(data.get("markdown") or "")), body


def process_business(
    db: Any,
    firecrawl: Any,
    business: dict[str, Any],
    *,
    map_limit: int,
    map_timeout_ms: int,
    apply: bool,
) -> dict[str, Any]:
    business_id = int(business["business_id"])
    website = str(business.get("website") or "").strip()
    result: dict[str, Any] = {
        "business_id": business_id,
        "website": website,
        "mapped": 0,
        "promotion_urls": 0,
        "scraped": 0,
        "promotions_upserted": 0,
        "errors": [],
    }
    try:
        mapped_urls = map_site(firecrawl, website, limit=map_limit, timeout_ms=map_timeout_ms)
    except Exception as exc:
        result["errors"].append(f"map: {exc}")
        return result

    promotion_urls = discover_promotion_urls(website, mapped_urls)
    result["mapped"] = len(mapped_urls)
    result["promotion_urls"] = len(promotion_urls)
    if not apply:
        return result

    for source_url in promotion_urls:
        fingerprint = scrape_request_fingerprint(source_url)
        try:
            _markdown, response_json = scrape_and_capture(firecrawl, source_url)
            save_scrape_response(
                db,
                fingerprint,
                source_url,
                response_json,
                success=True,
            )
            upsert_promotion(db, business_id=business_id, source_url=source_url)
            result["scraped"] += 1
            result["promotions_upserted"] += 1
        except Exception as exc:
            try:
                save_scrape_response(
                    db,
                    fingerprint,
                    source_url,
                    None,
                    success=False,
                    error_message=str(exc),
                )
            except Exception as persist_exc:
                result["errors"].append(f"{source_url}: scrape: {exc}; raw_save: {persist_exc}")
                continue
            result["errors"].append(f"{source_url}: scrape: {exc}")
    return result


def _select_businesses(db: Any, business_ids: Sequence[int]) -> list[dict[str, Any]]:
    rows = fetch_all_rows(db, "master_business_info", "business_id,website", order="business_id.asc")
    wanted = set(business_ids)
    return [
        row
        for row in rows
        if str(row.get("website") or "").strip() and (not wanted or int(row["business_id"]) in wanted)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist Scrape raw rows and clinic promotions.")
    parser.add_argument("--business-id", action="append", type=int, default=[], help="Restrict to one or more business IDs.")
    parser.add_argument("--map-limit", type=int, default=500)
    parser.add_argument("--map-timeout-ms", type=int, default=60000)
    parser.add_argument("--firecrawl-api-url", default="", help="Override Firecrawl base URL for this run.")
    args = parser.parse_args()
    if args.map_limit <= 0 or args.map_timeout_ms <= 0:
        parser.error("map limits and timeouts must be positive")

    if args.firecrawl_api_url:
        os.environ["FIRECRAWL_API_URL"] = args.firecrawl_api_url.rstrip("/")
    db = load_supabase_client()
    firecrawl = get_firecrawl_client()
    businesses = _select_businesses(db, args.business_id)
    results = [
        process_business(
            db,
            firecrawl,
            business,
            map_limit=args.map_limit,
            map_timeout_ms=args.map_timeout_ms,
            apply=args.apply,
        )
        for business in businesses
    ]
    summary = {
        "apply": args.apply,
        "businesses": len(results),
        "mapped": sum(row["mapped"] for row in results),
        "promotion_urls": sum(row["promotion_urls"] for row in results),
        "scraped": sum(row["scraped"] for row in results),
        "promotions_upserted": sum(row["promotions_upserted"] for row in results),
        "failed_businesses": sum(bool(row["errors"]) for row in results),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["failed_businesses"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
