#!/usr/bin/env python3
"""
Discover missing price-bearing subpages for gap domains via Firecrawl crawl API.

Gap domain = at least one promo_website_staging row with no matching Website offer.
Read-only by default: outputs JSON/CSV reports, does not write to Supabase.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import FIRECRAWL_CRAWL_MAX_PAGES, FIRECRAWL_CRAWL_TIMEOUT_SECS, OUTPUT_DIR
from crawler.promo_site_crawler import normalize_domain, score_candidate_link, should_exclude_candidate
from crawler.staging_recrawl import (
    SupabaseRestClient,
    canonicalize_page_url,
    fetch_all_rows,
    load_supabase_client,
    recrawl_domain_via_firecrawl,
)
from scripts.audit_promo_website_staging import analyze_offer_master_coverage
from utils.staging_content_diff import has_price_signal

REPORT_PREFIX = "staging_price_page_gaps"
DOMAIN_REPORT_PREFIX = "staging_price_page_gap_domains"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Firecrawl crawl: find missing price subpages for staging gap domains"
    )
    parser.add_argument("--domain", default=None, help="Process a single domain only")
    parser.add_argument("--limit", type=int, default=None, help="Max gap domains to crawl")
    parser.add_argument(
        "--max-crawl-pages",
        type=int,
        default=FIRECRAWL_CRAWL_MAX_PAGES,
        help="Firecrawl crawl page limit per domain",
    )
    parser.add_argument(
        "--crawl-timeout-secs",
        type=int,
        default=FIRECRAWL_CRAWL_TIMEOUT_SECS,
        help="Firecrawl crawl timeout per domain",
    )
    parser.add_argument(
        "--min-url-score",
        type=int,
        default=0,
        help="Minimum score_candidate_link score (0 = content price signal only)",
    )
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Report output directory")
    return parser.parse_args()


def filter_price_promo_rows(
    crawl_rows: Sequence[Dict[str, Any]],
    *,
    min_url_score: int,
) -> List[Dict[str, Any]]:
    """Keep crawl rows with price content and non-excluded URLs."""
    kept: List[Dict[str, Any]] = []
    for row in crawl_rows:
        url = str(row.get("subpage_url") or "").strip()
        if not url or should_exclude_candidate(url):
            continue
        content = str(row.get("page_content") or "")
        if not has_price_signal(content):
            continue
        url_score = score_candidate_link(url)
        if url_score < min_url_score:
            continue
        kept.append({**row, "url_score": url_score})
    return kept


def build_gap_domain_set(
    staging_rows: Sequence[Dict[str, Any]],
    staging_without_offers: Sequence[Dict[str, Any]],
    *,
    single_domain: Optional[str] = None,
) -> Tuple[List[str], Dict[str, int]]:
    """Return sorted gap domains and per-domain staging_without_offer counts."""
    without_by_domain: Counter[str] = Counter()
    for row in staging_without_offers:
        domain = normalize_domain(str(row.get("domain_name") or ""))
        if domain:
            without_by_domain[domain] += 1

    if single_domain:
        domain = normalize_domain(single_domain)
        if not domain:
            raise ValueError(f"Invalid domain: {single_domain!r}")
        return [domain], dict(without_by_domain)

    gap_domains = sorted(without_by_domain.keys())
    if not gap_domains:
        # Fallback: derive from staging rows if coverage returned empty
        all_domains = sorted(
            {
                normalize_domain(str(r.get("domain_name") or ""))
                for r in staging_rows
                if normalize_domain(str(r.get("domain_name") or ""))
            }
        )
        gap_domains = all_domains
    return gap_domains, dict(without_by_domain)


def build_existing_staging_index(
    staging_rows: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """domain_name -> canonical_url -> staging row."""
    by_domain: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for row in staging_rows:
        domain = normalize_domain(str(row.get("domain_name") or ""))
        canonical = canonicalize_page_url(str(row.get("subpage_url") or ""))
        if domain and canonical:
            by_domain[domain][canonical] = row
    return by_domain


def find_missing_candidates(
    filtered_rows: Sequence[Dict[str, Any]],
    existing_by_url: Dict[str, Dict[str, Any]],
    *,
    domain_name: str,
    seed_website_url: str,
    staging_without_offer_count: int,
) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []
    for row in filtered_rows:
        url = str(row.get("subpage_url") or "")
        canonical = canonicalize_page_url(url)
        if not canonical or canonical in existing_by_url:
            continue
        content = str(row.get("page_content") or "")
        missing.append(
            {
                "domain_name": domain_name,
                "subpage_url": url,
                "canonical_subpage_url": canonical,
                "url_score": row.get("url_score", score_candidate_link(url)),
                "page_content_length": len(content),
                "has_price_signal": True,
                "seed_website_url": seed_website_url,
                "existing_staging_count": len(existing_by_url),
                "staging_without_offer_count": staging_without_offer_count,
            }
        )
    return missing


def process_domain(
    domain_name: str,
    client: SupabaseRestClient,
    existing_by_domain: Dict[str, Dict[str, Dict[str, Any]]],
    staging_without_offer_count: int,
    *,
    max_crawl_pages: int,
    crawl_timeout_secs: int,
    min_url_score: int,
) -> Dict[str, Any]:
    started = time.monotonic()
    result: Dict[str, Any] = {
        "domain_name": domain_name,
        "error": None,
        "crawl_status": None,
        "crawl_total": None,
        "crawl_completed": None,
        "seed_website_url": None,
        "crawl_rows_total": 0,
        "filtered_price_rows": 0,
        "missing_candidates": [],
        "elapsed_secs": 0.0,
    }
    try:
        target, crawl_rows, run_meta = recrawl_domain_via_firecrawl(
            domain_name,
            client=client,
            max_crawl_pages=max_crawl_pages,
            crawl_timeout_secs=crawl_timeout_secs,
        )
        result["seed_website_url"] = target.website_url
        result["crawl_status"] = run_meta.get("crawl_status")
        result["crawl_total"] = run_meta.get("crawl_total")
        result["crawl_completed"] = run_meta.get("crawl_completed")
        result["crawl_rows_total"] = len(crawl_rows)

        filtered = filter_price_promo_rows(crawl_rows, min_url_score=min_url_score)
        result["filtered_price_rows"] = len(filtered)

        existing = existing_by_domain.get(domain_name, {})
        result["missing_candidates"] = find_missing_candidates(
            filtered,
            existing,
            domain_name=domain_name,
            seed_website_url=target.website_url,
            staging_without_offer_count=staging_without_offer_count,
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    result["elapsed_secs"] = round(time.monotonic() - started, 2)
    return result


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: List[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_summary(domain_results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    flat_missing = [
        candidate
        for domain_result in domain_results
        for candidate in (domain_result.get("missing_candidates") or [])
    ]
    domains_with_candidates = sum(
        1 for r in domain_results if (r.get("missing_candidates") or []) and not r.get("error")
    )
    top_domains = sorted(
        (
            (r["domain_name"], len(r.get("missing_candidates") or []))
            for r in domain_results
            if not r.get("error")
        ),
        key=lambda item: item[1],
        reverse=True,
    )[:20]
    return {
        "gap_domains_processed": len(domain_results),
        "gap_domains_with_new_candidates": domains_with_candidates,
        "gap_domains_with_errors": sum(1 for r in domain_results if r.get("error")),
        "total_missing_price_pages": len(flat_missing),
        "total_crawl_pages_scanned": sum(int(r.get("crawl_rows_total") or 0) for r in domain_results),
        "total_filtered_price_pages": sum(int(r.get("filtered_price_rows") or 0) for r in domain_results),
        "top_domains_by_new_candidates": top_domains,
    }


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    output_dir = Path(args.output_dir).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    client = load_supabase_client()
    staging_rows = fetch_all_rows(
        client,
        "promo_website_staging",
        "promo_website_id,domain_name,name,subpage_url,page_content,processed_status",
        order="promo_website_id.asc",
    )
    offer_rows = fetch_all_rows(
        client,
        "promo_offer_master",
        "id,channel,source_url,source_name,template_type,service_name,status",
        order="id.asc",
    )

    _, coverage_summary, staging_without_offers, _ = analyze_offer_master_coverage(staging_rows, offer_rows)
    gap_domains, without_counts = build_gap_domain_set(
        staging_rows,
        staging_without_offers,
        single_domain=args.domain,
    )
    if args.limit is not None:
        gap_domains = gap_domains[: args.limit]

    existing_by_domain = build_existing_staging_index(staging_rows)
    domain_results: List[Dict[str, Any]] = []
    for idx, domain in enumerate(gap_domains, start=1):
        print(
            f"[{idx}/{len(gap_domains)}] crawling {domain} "
            f"(staging_without_offer={without_counts.get(domain, 0)})",
            flush=True,
        )
        domain_results.append(
            process_domain(
                domain,
                client,
                existing_by_domain,
                without_counts.get(domain, 0),
                max_crawl_pages=args.max_crawl_pages,
                crawl_timeout_secs=args.crawl_timeout_secs,
                min_url_score=args.min_url_score,
            )
        )
        last = domain_results[-1]
        print(
            f"  crawl_rows={last.get('crawl_rows_total')} "
            f"filtered={last.get('filtered_price_rows')} "
            f"missing={len(last.get('missing_candidates') or [])} "
            f"elapsed={last.get('elapsed_secs')}s "
            f"error={last.get('error')}",
            flush=True,
        )

    flat_candidates = [
        candidate
        for domain_result in domain_results
        for candidate in (domain_result.get("missing_candidates") or [])
    ]
    domain_summaries = [
        {
            "domain_name": r.get("domain_name"),
            "seed_website_url": r.get("seed_website_url"),
            "crawl_rows_total": r.get("crawl_rows_total"),
            "filtered_price_rows": r.get("filtered_price_rows"),
            "missing_candidate_count": len(r.get("missing_candidates") or []),
            "staging_without_offer_count": without_counts.get(r.get("domain_name", ""), 0),
            "elapsed_secs": r.get("elapsed_secs"),
            "error": r.get("error"),
        }
        for r in domain_results
    ]
    summary = build_summary(domain_results)

    json_path = output_dir / f"{REPORT_PREFIX}_{timestamp}.json"
    csv_path = output_dir / f"{REPORT_PREFIX}_{timestamp}.csv"
    domain_csv_path = output_dir / f"{DOMAIN_REPORT_PREFIX}_{timestamp}.csv"

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "coverage_summary": coverage_summary,
        "gap_domains_requested": len(gap_domains),
        "summary": summary,
        "domain_results": domain_results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, flat_candidates)
    write_csv(domain_csv_path, domain_summaries)

    print(json.dumps({"summary": summary, "json_path": str(json_path), "csv_path": str(csv_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
