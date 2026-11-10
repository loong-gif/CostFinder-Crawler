#!/usr/bin/env python3
"""Evidence-first full-table repair for ``clinic_memberships``.

The command defaults to a dry-run.  It snapshots the live rows, re-scrapes each
distinct source URL through the self-hosted Firecrawl API, extracts current
membership facts, and writes a complete action plan.  ``--apply`` consumes that
plan and performs only the already classified, foreign-key-safe writes.
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

from utils.membership_repair_apply import apply_plan
from utils.membership_repair_common import DEFAULT_FIRECRAWL_URL, now_stamp, read_json, source_key, write_json
from utils.membership_repair_extraction import candidate_links, extract_many, extraction_plans, source_root
from utils.membership_repair_plan import build_dry_run
from utils.membership_repair_scrape import FirecrawlHTTP, scrape_many
from utils.offer_extraction_llm import build_client_from_env
from utils.schema_contract import (
    CLINIC_MEMBERSHIP_SELECT,
    OFFER_MASTER_WITH_ITEMS_SELECT,
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_access import fetch_all_rows, load_supabase_client

ARTIFACT_ROOT = PROJECT_ROOT / ".firecrawl"


def snapshot_database(client: Any) -> Dict[str, Any]:
    memberships = fetch_all_rows(client, TABLE_CLINIC_MEMBERSHIPS, CLINIC_MEMBERSHIP_SELECT, order="plan_id.asc")
    offers = fetch_all_rows(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_WITH_ITEMS_SELECT, order="id.asc")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "memberships": memberships,
        "offers": offers,
        "membership_count": len(memberships),
        "distinct_source_url_count": len({source_key(row.get("source_url")) for row in memberships if source_key(row.get("source_url"))}),
        "schema_columns": sorted(memberships[0].keys()) if memberships else [],
    }


def run_dry_run(
    artifact_dir: Path,
    *,
    scrape_workers: int = 1,
    extraction_workers: int = 1,
    request_interval: float = 1.0,
    extraction_interval: float = 2.0,
) -> Path:
    load_dotenv(PROJECT_ROOT / ".env")
    client = load_supabase_client(PROJECT_ROOT)
    snapshot = snapshot_database(client)
    if snapshot["membership_count"] == 0 or snapshot["distinct_source_url_count"] == 0:
        raise RuntimeError("live membership snapshot is empty")
    write_json(artifact_dir / "snapshot.json", snapshot)

    api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is required")
    fc = FirecrawlHTTP(
        os.getenv("COSTFINDER_FIRECRAWL_URL", DEFAULT_FIRECRAWL_URL),
        api_key,
        request_interval=request_interval,
    )
    urls = sorted({source_key(row.get("source_url")) for row in snapshot["memberships"]})
    main_pages = scrape_many(fc, urls, max_workers=scrape_workers)
    write_json(artifact_dir / "firecrawl_main_scrapes.json", main_pages)

    llm = build_client_from_env()
    if llm is None:
        raise RuntimeError("LLM_API_URL/LLM_API_KEY/LLM_MODEL are required for current-page extraction")
    main_extractions = extract_many(
        llm,
        main_pages,
        max_workers=extraction_workers,
        min_interval=extraction_interval,
    )
    write_json(artifact_dir / "main_extractions.json", main_extractions)

    main_by_key = {(source_key(item.get("requested_url")), bool(item.get("fallback"))): item for item in main_extractions}
    fallback_candidates: Dict[str, List[str]] = {}
    for page in main_pages:
        key = source_key(page.get("requested_url"))
        extraction = main_by_key.get((key, False), {})
        if extraction_plans(extraction):
            continue
        try:
            mapped = fc.map(source_root(key))
            fallback_candidates[key] = candidate_links(mapped, key)
        except Exception as exc:
            fallback_candidates[key] = []
            write_json(artifact_dir / f"map_error_{len(fallback_candidates):03d}.json", {"url": key, "error": str(exc)})
    write_json(artifact_dir / "map_candidates.json", fallback_candidates)

    fallback_urls = sorted({url for urls_for_source in fallback_candidates.values() for url in urls_for_source})
    fallback_pages = (
        scrape_many(fc, fallback_urls, fallback=True, max_workers=scrape_workers)
        if fallback_urls
        else []
    )
    write_json(artifact_dir / "firecrawl_fallback_scrapes.json", fallback_pages)
    fallback_extractions = (
        extract_many(
            llm,
            fallback_pages,
            max_workers=extraction_workers,
            min_interval=extraction_interval,
        )
        if fallback_pages
        else []
    )
    write_json(artifact_dir / "fallback_extractions.json", fallback_extractions)

    all_pages = main_pages + fallback_pages
    all_extractions = main_extractions + fallback_extractions
    dry_run = build_dry_run(snapshot, all_pages, all_extractions, artifact_dir=artifact_dir)
    write_json(artifact_dir / "dry_run.json", dry_run)
    return artifact_dir / "dry_run.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="apply an existing dry-run plan")
    parser.add_argument("--resume", action="store_true", help="resume a partially applied dry-run plan")
    parser.add_argument("--input", type=Path, help="dry_run.json generated by the default mode")
    parser.add_argument("--artifact-dir", type=Path, help="output directory for a new dry-run")
    parser.add_argument("--scrape-workers", type=int, default=1)
    parser.add_argument("--extraction-workers", type=int, default=1)
    parser.add_argument("--request-interval", type=float, default=1.0)
    parser.add_argument("--extraction-interval", type=float, default=2.0)
    args = parser.parse_args()
    if args.apply:
        if not args.input:
            parser.error("--apply requires --input")
        print(json.dumps(apply_plan(args.input, resume=args.resume), ensure_ascii=False, indent=2))
        return 0
    artifact_dir = args.artifact_dir or (ARTIFACT_ROOT / f"clinic_memberships_repair_{now_stamp()}")
    path = run_dry_run(
        artifact_dir,
        scrape_workers=args.scrape_workers,
        extraction_workers=args.extraction_workers,
        request_interval=args.request_interval,
        extraction_interval=args.extraction_interval,
    )
    dry_run = read_json(path)
    print(json.dumps({"dry_run": str(path), "membership_count": dry_run["membership_count"], "action_counts": dry_run["action_counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
