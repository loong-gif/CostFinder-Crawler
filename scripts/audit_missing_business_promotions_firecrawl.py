#!/usr/bin/env python3
"""Audit promotion-free businesses with self-hosted Firecrawl and Gemini.

The command never writes ``clinic_promotions``.  ``--write-raw`` only permits
upserts to ``firecrawl_scrape_raw`` so every accepted audit result can point to
the page evidence that produced it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.gemini_client import build_gemini_client
from utils.promo_paths import normalized_host as _normalized_host
from utils.promo_extraction import llm_extract_promotions, merge_promotion_items
from utils.firecrawl_scrape_raw_db import canonical_scrape_url
from utils.missing_business_audit_attribution import (
    activity_from_dates,
    assign_businesses,
    has_explicit_promotion_signal,
)
from utils.missing_business_audit_firecrawl import (
    RawSpoolDb,
    SelfHostedFirecrawl,
    _persist_raw,
    _same_host_promotion_urls,
    _site_root,
)
from utils.missing_business_audit_reports import (
    build_audit,
    select_missing_businesses,
    write_raw_spool,
    write_reports,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key

DEFAULT_FIRECRAWL_API_URL = "http://192.168.1.68:3002"
DEFAULT_MAP_LIMIT = 500
DEFAULT_WORKERS = 3
FALLBACK_PATHS = ("/special", "/specials", "/promotion", "/promotions")


def _page_data(response: Mapping[str, Any]) -> dict[str, Any]:
    nested = response.get("data")
    return dict(nested) if isinstance(nested, Mapping) else dict(response)


def _empty_result(business: Mapping[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    return {
        "business_id": int(business["business_id"]),
        "name": business.get("name"),
        "website": business.get("website"),
        "host": _normalized_host(business.get("website")),
        "status": status,
        "candidate_urls": [],
        "promotions": [],
        "raw_ids": [],
        "errors": [],
        **extra,
    }


def _audit_host(
    *,
    db: Any,
    firecrawl: Any,
    gemini: Any,
    targets: Sequence[dict[str, Any]],
    host_members: Sequence[dict[str, Any]],
    map_limit: int,
    write_raw: bool,
    today: date,
    schema: dict[str, Any],
    raw_lock: threading.Lock,
) -> list[dict[str, Any]]:
    website = str(targets[0].get("website") or "")
    base_results = {
        int(row["business_id"]): _empty_result(row, "no_qualifying_promotion_found")
        for row in targets
    }
    try:
        mapped = firecrawl.map(_site_root(website), limit=map_limit)
    except Exception as exc:
        return [
            _empty_result(row, "map_failed", errors=[str(exc)[:1000]])
            for row in targets
        ]

    candidate_urls = _same_host_promotion_urls(website, mapped)
    if not candidate_urls:
        candidate_urls = [canonical_scrape_url(_site_root(website).rstrip("/") + path) for path in FALLBACK_PATHS]
    for result in base_results.values():
        result["candidate_urls"] = list(candidate_urls)

    matched_promotions: dict[int, list[dict[str, Any]]] = {
        int(row["business_id"]): [] for row in targets
    }
    unresolved_pages: list[dict[str, Any]] = []
    scrape_errors: list[str] = []
    gemini_errors: list[str] = []
    raw_errors: list[str] = []
    raw_ids: list[int] = []

    for source_url in candidate_urls:
        try:
            response = firecrawl.scrape(source_url)
        except Exception as exc:
            error = f"{source_url}: {str(exc)[:800]}"
            scrape_errors.append(error)
            if write_raw:
                try:
                    raw_ids.append(
                        _persist_raw(
                            db,
                            raw_lock,
                            source_url,
                            None,
                            success=False,
                            error_message=str(exc)[:1000],
                        )
                    )
                except Exception as persist_exc:
                    raw_errors.append(f"{source_url}: {str(persist_exc)[:800]}")
            continue

        raw_id: int | None = None
        if write_raw:
            try:
                raw_id = _persist_raw(
                    db,
                    raw_lock,
                    source_url,
                    response,
                    success=True,
                    error_message=None,
                )
                raw_ids.append(raw_id)
            except Exception as exc:
                raw_errors.append(f"{source_url}: {str(exc)[:800]}")
                continue

        data = _page_data(response)
        evidence = str(data.get("markdown") or "")
        metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
        page_status = metadata.get("statusCode") or metadata.get("status_code")
        if page_status is not None:
            try:
                if int(page_status) >= 400:
                    continue
            except (TypeError, ValueError):
                pass
        if not has_explicit_promotion_signal(evidence):
            continue
        try:
            extracted = llm_extract_promotions(
                gemini,
                source_url=source_url,
                evidence=evidence,
                schema=schema,
            )
            items = list(extracted.get("promotions") or [])
            if not items:
                continue
            merged = merge_promotion_items(
                items,
                evidence,
                webpage_title=str(metadata.get("title") or ""),
                source_url=source_url,
            )
        except ValueError as exc:
            if str(exc) == "no validated promotions":
                continue
            gemini_errors.append(f"{source_url}: {str(exc)[:800]}")
            continue
        except Exception as exc:
            gemini_errors.append(f"{source_url}: {str(exc)[:800]}")
            continue

        business_ids, attribution = assign_businesses(source_url, evidence, host_members)
        promotion_evidence = "\n".join(
            [
                str(merged.get("promotion_title") or ""),
                *(str(item) for item in merged.get("promotion_content") or []),
            ]
        )
        activity = activity_from_dates(
            merged.get("campaign_start_date"),
            merged.get("campaign_end_date"),
            promotion_evidence,
            today=today,
        )
        promotion = {
            "source_url": source_url,
            "raw_id": raw_id,
            "promotion_title": merged.get("promotion_title"),
            "promotion_content": list(merged.get("promotion_content") or []),
            "campaign_start_date": merged.get("campaign_start_date"),
            "campaign_end_date": merged.get("campaign_end_date"),
            "is_active": activity["is_active"],
            "attribution": attribution,
        }
        if not business_ids:
            unresolved_pages.append(promotion)
            continue
        target_ids = set(base_results)
        for business_id in business_ids:
            if business_id in target_ids:
                matched_promotions[business_id].append(dict(promotion))

    for business_id, result in base_results.items():
        result["raw_ids"] = sorted(set(raw_ids))
        result["errors"] = [*raw_errors, *scrape_errors, *gemini_errors]
        promotions = matched_promotions[business_id]
        if promotions:
            result["promotions"] = promotions
            result["status"] = (
                "date_review_required"
                if any(item.get("is_active") is None for item in promotions)
                else "promotion_found"
            )
        elif unresolved_pages:
            result["status"] = "shared_host_unresolved"
            result["unresolved_promotions"] = unresolved_pages
        elif raw_errors or scrape_errors:
            result["status"] = "scrape_failed"
        elif gemini_errors:
            result["status"] = "gemini_failed"
    return [base_results[int(row["business_id"])] for row in targets]


def audit_businesses(
    *,
    db: Any,
    firecrawl: Any,
    gemini: Any,
    targets: Sequence[dict[str, Any]],
    all_businesses: Sequence[dict[str, Any]],
    map_limit: int = DEFAULT_MAP_LIMIT,
    workers: int = 1,
    write_raw: bool = False,
    today: date | None = None,
    schema: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if map_limit < 1 or workers < 1:
        raise ValueError("map_limit and workers must be positive")
    current_day = today or date.today()
    extraction_schema = schema or json.loads(
        (PROJECT_ROOT / "schema" / "promotion_extraction_schema.json").read_text(encoding="utf-8")
    )
    results = [
        _empty_result(row, "missing_website")
        for row in targets
        if not str(row.get("website") or "").strip()
    ]
    targets_by_host: dict[str, list[dict[str, Any]]] = {}
    for row in targets:
        host = _normalized_host(row.get("website"))
        if host:
            targets_by_host.setdefault(host, []).append(dict(row))
    members_by_host: dict[str, list[dict[str, Any]]] = {}
    for row in all_businesses:
        host = _normalized_host(row.get("website"))
        if host:
            members_by_host.setdefault(host, []).append(dict(row))

    raw_lock = threading.Lock()
    jobs = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for host, host_targets in targets_by_host.items():
            jobs[
                executor.submit(
                    _audit_host,
                    db=db,
                    firecrawl=firecrawl,
                    gemini=gemini,
                    targets=host_targets,
                    host_members=members_by_host.get(host, host_targets),
                    map_limit=map_limit,
                    write_raw=write_raw,
                    today=current_day,
                    schema=extraction_schema,
                    raw_lock=raw_lock,
                )
            ] = host
        for future in as_completed(jobs):
            results.extend(future.result())
    return sorted(results, key=lambda row: int(row["business_id"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", action="append", type=int, default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--map-limit", type=int, default=DEFAULT_MAP_LIMIT)
    parser.add_argument("--firecrawl-api-url", default=DEFAULT_FIRECRAWL_API_URL)
    parser.add_argument("--write-raw", action="store_true")
    parser.add_argument(
        "--mcp-stdin",
        action="store_true",
        help="Read targets/all_businesses JSON from stdin and spool raw rows for MCP import.",
    )
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / ".firecrawl"))
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.workers < 1 or args.map_limit < 1:
        parser.error("--workers and --map-limit must be positive")

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    if args.mcp_stdin:
        payload = json.load(sys.stdin)
        all_businesses = [dict(row) for row in payload.get("all_businesses") or []]
        targets = [dict(row) for row in payload.get("targets") or []]
        wanted = {int(value) for value in args.business_id}
        if wanted:
            targets = [row for row in targets if int(row["business_id"]) in wanted]
        if args.limit is not None:
            targets = targets[: args.limit]
        db: Any = RawSpoolDb()
    else:
        supabase_url = str(os.getenv("SUPABASE_URL") or "").strip()
        if not supabase_url:
            raise RuntimeError("Missing SUPABASE_URL")
        db = SupabaseRestClient(supabase_url, get_supabase_writer_key())
    firecrawl = SelfHostedFirecrawl(
        args.firecrawl_api_url,
        str(os.getenv("FIRECRAWL_API_KEY") or "").strip(),
    )
    if not args.mcp_stdin:
        try:
            targets, all_businesses = select_missing_businesses(
                db,
                business_ids=tuple(args.business_id) or None,
                limit=args.limit,
            )
        except requests.HTTPError as exc:
            raise RuntimeError(
                "Configured SUPABASE_URL cannot read the required costfinder tables; "
                "verify that SUPABASE_URL and the writer credential belong to the same project"
            ) from exc
    firecrawl.healthcheck()
    gemini = build_gemini_client()
    results = audit_businesses(
        db=db,
        firecrawl=firecrawl,
        gemini=gemini,
        targets=targets,
        all_businesses=all_businesses,
        map_limit=args.map_limit,
        workers=args.workers,
        write_raw=args.write_raw,
    )
    audit = build_audit(results, write_raw=args.write_raw)
    output_dir = Path(args.output_dir)
    json_path, csv_path = write_reports(audit, output_dir)
    raw_spool_path = (
        write_raw_spool(db, output_dir, audit["run_date"])
        if args.mcp_stdin and args.write_raw
        else None
    )
    print(
        json.dumps(
            {
                "businesses": audit["businesses"],
                "status_counts": audit["status_counts"],
                "balanced": audit["balanced"],
                "json_output": str(json_path),
                "csv_output": str(csv_path),
                "raw_spool_output": str(raw_spool_path) if raw_spool_path else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
