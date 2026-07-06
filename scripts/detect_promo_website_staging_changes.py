#!/usr/bin/env python3
"""
Detect page_content changes for promo_website_staging rows by recrawling subpage_url.

Default: dry-run report only. Use --apply to write changed rows back to Supabase.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.page_content_processor import process_page_content
from utils.staging_content_diff import classify_content_change
from scripts.audit_promo_website_staging import normalize_url, normalize_domain
from urllib.parse import urlparse

TABLE = "promo_website_staging"
OFFER_TABLE = "promo_offer_master"
# ponytail: original_price 不在表里；与 change_driven_extractor 同序回退。
OFFER_SELECT_VARIANTS = [
    "id,service_name,offer_raw_text,regular_price,discount_price,status,source_url",
    "id,service_name,offer_raw_text,discount_price,status,source_url",
    "id,service_name,offer_raw_text,status,source_url",
]
REPORT_PREFIX = "promo_website_staging_change_detect"
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 1000
STAGING_SELECT = (
    "promo_website_id,subpage_url,domain_name,page_content,crawl_timestamp,processed_status,name"
)


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.raw_base_url = base_url.rstrip("/")
        self.base_url = self.raw_base_url + "/rest/v1"
        self.service_role_key = service_role_key
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"select": select}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        if order:
            params["order"] = order
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=90)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_err = exc
                if attempt < 2:
                    import time
                    time.sleep(2 * (attempt + 1))
        raise last_err  # type: ignore[misc]

    def update_row(self, table: str, filters: Dict[str, str], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = self.session.patch(
            f"{self.base_url}/{table}",
            params=filters,
            headers={"Prefer": "return=representation"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect promo_website_staging page_content changes via Firecrawl recrawl")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N rows")
    parser.add_argument("--domain", default=None, help="Only process rows for this domain_name")
    parser.add_argument("--id", type=int, default=None, dest="row_id", help="Only process one promo_website_id")
    parser.add_argument("--concurrency", type=int, default=5, help="Firecrawl fetch concurrency (default: 5)")
    parser.add_argument("--join-offers", action="store_true", help="Link changed URLs to promo_offer_master.source_url")
    parser.add_argument(
        "--from-report",
        default=None,
        help="Skip recrawl; load results from an existing JSON report and optionally --join-offers",
    )
    parser.add_argument("--apply", action="store_true", help="Write changed rows back to Supabase")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Report output directory")
    return parser.parse_args()


def fetch_staging_rows(client: SupabaseRestClient, args: argparse.Namespace) -> List[Dict[str, Any]]:
    filters: Dict[str, str] = {}
    if args.domain:
        filters["domain_name"] = f"eq.{args.domain.strip().lower()}"
    if args.row_id is not None:
        filters["promo_website_id"] = f"eq.{args.row_id}"

    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch_limit = PAGE_SIZE
        if args.limit is not None:
            remaining = args.limit - len(rows)
            if remaining <= 0:
                break
            batch_limit = min(batch_limit, remaining)

        batch = client.fetch_rows(
            TABLE,
            STAGING_SELECT,
            filters=filters or None,
            limit=batch_limit,
            offset=offset,
            order="promo_website_id.asc",
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < batch_limit:
            break
        offset += len(batch)
        if args.limit is not None and len(rows) >= args.limit:
            break
    return rows


def build_row_result(row: Dict[str, Any], crawl: Dict[str, Any]) -> Dict[str, Any]:
    old_content = str(row.get("page_content") or "")
    base = {
        "promo_website_id": row.get("promo_website_id"),
        "subpage_url": row.get("subpage_url"),
        "domain_name": row.get("domain_name"),
        "old_content_len": len(old_content),
    }

    if not crawl.get("success"):
        return {
            **base,
            "change_type": "crawl_failed",
            "needs_review": True,
            "price_signal_lost": False,
            "price_signal_gained": False,
            "error_message": crawl.get("error_message", ""),
            "new_content_len": 0,
            "old_content_preview": old_content[:300],
            "new_content_preview": "",
        }

    new_content = str(crawl.get("page_content") or "")
    diff = classify_content_change(old_content, new_content)
    needs_review = diff.change_type in {"changed", "empty_new"} or diff.price_signal_lost

    return {
        **base,
        "change_type": diff.change_type,
        "needs_review": needs_review,
        "price_signal_lost": diff.price_signal_lost,
        "price_signal_gained": diff.price_signal_gained,
        "old_hash": diff.old_hash,
        "new_hash": diff.new_hash,
        "new_content_len": diff.new_len,
        "old_content_preview": old_content[:300],
        "new_content_preview": new_content[:300],
        "new_page_content": new_content,
        "processed": crawl.get("processed"),
        "error_message": "",
    }


async def fetch_page_content_firecrawl(url: str, engine) -> Dict[str, Any]:
    try:
        page = await engine.fetch(url)
        processed = process_page_content(page.content or "", source_type="markdown")
        page_content = processed.get("page_content") or processed.get("page_content_llm") or ""
        if not str(page_content).strip():
            return {"success": False, "page_content": "", "error_message": "empty_content_after_processing"}
        return {
            "success": True,
            "page_content": page_content,
            "processed": processed,
            "error_message": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "page_content": "", "error_message": str(exc)}


async def recrawl_rows(rows: List[Dict[str, Any]], concurrency: int) -> List[Dict[str, Any]]:
    from crawler.fetch_engine import FirecrawlFetchEngine

    sem = asyncio.Semaphore(max(1, concurrency))
    engine = FirecrawlFetchEngine()

    async def one(row: Dict[str, Any]) -> Dict[str, Any]:
        url = str(row.get("subpage_url") or "").strip()
        if not url:
            crawl = {"success": False, "page_content": "", "error_message": "missing_subpage_url"}
            return build_row_result(row, crawl)
        async with sem:
            crawl = await fetch_page_content_firecrawl(url, engine)
            return build_row_result(row, crawl)

    return list(await asyncio.gather(*(one(row) for row in rows)))


def build_summary(results: List[Dict[str, Any]], *, mode: str) -> Dict[str, Any]:
    counter = Counter(item.get("change_type", "unknown") for item in results)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "total_rows": len(results),
        "unchanged": counter.get("unchanged", 0),
        "changed": counter.get("changed", 0),
        "empty_old": counter.get("empty_old", 0),
        "empty_new": counter.get("empty_new", 0),
        "both_empty": counter.get("both_empty", 0),
        "crawl_failed": counter.get("crawl_failed", 0),
        "price_signal_lost": sum(1 for item in results if item.get("price_signal_lost")),
        "needs_review": sum(1 for item in results if item.get("needs_review")),
    }


def _load_offer_index(client: SupabaseRestClient) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[str]]]:
    """一次拉全表 website offer，建 (normalized_url -> offers) 与 (domain -> [normalized_urls]) 索引。

    ponytail: 比 per-URL eq 快且避开 select 列 400；395 行 1-2 次请求即可。
    """
    offers: List[Dict[str, Any]] = []
    offset = 0
    last_err: Optional[Exception] = None
    for select in OFFER_SELECT_VARIANTS:
        try:
            offers = []
            offset = 0
            while True:
                batch = client.fetch_rows(
                    OFFER_TABLE,
                    select,
                    filters={"channel": "eq.Website"},
                    limit=PAGE_SIZE,
                    offset=offset,
                )
                if not batch:
                    break
                offers.extend(batch)
                if len(batch) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
            break
        except requests.RequestException as exc:
            last_err = exc
            continue
    if not offers and last_err is not None:
        raise last_err

    by_url: Dict[str, List[Dict[str, Any]]] = {}
    by_domain: Dict[str, List[str]] = {}
    for row in offers:
        nu = normalize_url(row.get("source_url"))
        if not nu:
            continue
        by_url.setdefault(nu, []).append(row)
        dom = normalize_domain(urlparse(nu).netloc)
        if dom:
            by_domain.setdefault(dom, [])
            if nu not in by_domain[dom]:
                by_domain[dom].append(nu)
    return by_url, by_domain


def _classify_unmatched(url: str, domain_name: str, by_domain: Dict[str, List[str]]) -> str:
    nu = normalize_url(url)
    dom_urls = by_domain.get(normalize_domain(domain_name) if domain_name else "", [])
    if nu in dom_urls:
        return "url_format_diff"
    if not dom_urls:
        return "no_offers_on_domain"
    return "domain_has_other_urls"


def join_offers(
    by_url: Dict[str, List[Dict[str, Any]]],
    by_domain: Dict[str, List[str]],
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    stale_candidates: List[Dict[str, Any]] = []
    for item in results:
        if item.get("change_type") != "changed" and not item.get("price_signal_lost"):
            continue
        url = str(item.get("subpage_url") or "")
        nu = normalize_url(url)
        linked = by_url.get(nu, []) if nu else []
        item["linked_offer_ids"] = [row.get("id") for row in linked]
        item["linked_offer_count"] = len(linked)
        item["sample_offer_text"] = str(linked[0].get("offer_raw_text") or "")[:200] if linked else ""
        if not linked:
            item["unmatched_reason"] = _classify_unmatched(url, str(item.get("domain_name") or ""), by_domain)
            continue
        stale_candidates.append(
            {
                "source_url": url,
                "offer_ids": item["linked_offer_ids"],
                "change_type": item.get("change_type"),
                "price_signal_lost": item.get("price_signal_lost"),
            }
        )
    return stale_candidates


def write_reports(
    output_dir: Path,
    summary: Dict[str, Any],
    results: List[Dict[str, Any]],
    stale_candidates: List[Dict[str, Any]],
) -> Dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"{REPORT_PREFIX}_{timestamp}.json"
    csv_path = output_dir / f"{REPORT_PREFIX}_{timestamp}_changed.csv"
    classified_csv_path = output_dir / f"{REPORT_PREFIX}_{timestamp}_classified.csv"

    serializable_results = []
    for item in results:
        row = {k: v for k, v in item.items() if k not in {"processed", "new_page_content"}}
        serializable_results.append(row)

    payload = {
        "summary": summary,
        "stale_offer_candidates": stale_candidates,
        "results": serializable_results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    changed_rows = [
        item
        for item in serializable_results
        if item.get("change_type") == "changed" or item.get("price_signal_lost")
    ]
    fieldnames = [
        "promo_website_id",
        "subpage_url",
        "domain_name",
        "change_type",
        "price_signal_lost",
        "needs_review",
        "old_content_len",
        "new_content_len",
        "linked_offer_count",
        "sample_offer_text",
        "error_message",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(changed_rows)

    classified_fieldnames = [
        "promo_website_id",
        "subpage_url",
        "domain_name",
        "change_type",
        "linked_offer_count",
        "unmatched_reason",
        "needs_review",
        "price_signal_lost",
        "price_signal_gained",
        "sample_offer_text",
    ]
    with classified_csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=classified_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(changed_rows)

    return {
        "json_path": str(json_path),
        "csv_path": str(csv_path),
        "classified_csv_path": str(classified_csv_path),
    }


def apply_updates(client: SupabaseRestClient, results: List[Dict[str, Any]]) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    for item in results:
        if item.get("change_type") != "changed" or not item.get("new_page_content"):
            continue
        row_id = item.get("promo_website_id")
        if row_id is None:
            continue
        processed = item.get("processed") or {}
        payload: Dict[str, Any] = {
            "page_content": item["new_page_content"],
            "crawl_timestamp": now_iso,
            "processed_status": False,
        }
        if processed:
            payload["page_content_llm"] = processed.get("page_content_llm") or ""
            payload["page_segments_raw"] = json.dumps(
                processed.get("page_segments_raw") or [], ensure_ascii=False, separators=(",", ":")
            )
            payload["page_segments_filtered"] = json.dumps(
                processed.get("page_segments_filtered") or [], ensure_ascii=False, separators=(",", ":")
            )
            payload["content_quality_flags"] = json.dumps(
                processed.get("content_quality_flags") or [], ensure_ascii=False, separators=(",", ":")
            )
        client.update_row(TABLE, {"promo_website_id": f"eq.{row_id}"}, payload)
        updated += 1
    return updated


def load_results_from_report(path: str) -> List[Dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError(f"No results[] in report: {path}")
    return results


def main() -> None:
    args = parse_args()
    client = load_supabase_client()
    if args.from_report:
        results = load_results_from_report(args.from_report)
    else:
        rows = fetch_staging_rows(client, args)
        if not rows:
            print(json.dumps({"status": "no_rows", "message": "No staging rows matched filters"}, ensure_ascii=False))
            return
        results = asyncio.run(recrawl_rows(rows, args.concurrency))
    mode = "apply" if args.apply else "dry_run"
    summary = build_summary(results, mode=mode)
    stale_candidates: List[Dict[str, Any]] = []
    join_error = ""
    # Save recrawl report before join so an offer-lookup failure does not discard work.
    paths = write_reports(Path(args.output_dir), summary, results, stale_candidates)
    if args.join_offers:
        try:
            by_url, by_domain = _load_offer_index(client)
            stale_candidates = join_offers(by_url, by_domain, results)
            paths = write_reports(Path(args.output_dir), summary, results, stale_candidates)
        except Exception as exc:  # noqa: BLE001
            join_error = str(exc)

    updated_rows = 0
    if args.apply:
        updated_rows = apply_updates(client, results)
        summary["updated_rows"] = updated_rows

    print(
        json.dumps(
            {
                "status": mode,
                "summary": summary,
                "report_paths": paths,
                "updated_rows": updated_rows,
                "join_offers_error": join_error or None,
                "stale_offer_candidates": len(stale_candidates),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
