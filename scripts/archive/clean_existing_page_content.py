#!/usr/bin/env python3
"""
Batch-clean existing page_content in promo_website_staging.

Reads all (or filtered) rows, runs content through the project's
segmentation pipeline (process_page_content), and writes back only
the cleaned page_content + processed_status=False.

The new page_content is built from the filtered segments' text content
(without [SEGMENT N] markers), keeping only what passed the price/promo
scoring pipeline. If no segments survive filtering, falls back to original.

Usage:
    python scripts/clean_existing_page_content.py --dry-run --limit 20
    python scripts/clean_existing_page_content.py --dry-run --id 123
    python scripts/clean_existing_page_content.py --limit 50
    python scripts/clean_existing_page_content.py --domain some-clinic.com
    python scripts/clean_existing_page_content.py  # all rows
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.page_content_processor import process_page_content
from utils.supabase_rest import SupabaseRestClient

TABLE = "promo_website_staging"
PAGE_SIZE = 200
STAGING_SELECT = "promo_website_id,subpage_url,domain_name,page_content,processed_status"
REPORT_PREFIX = "clean_existing_page_content"
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
MIN_CONTENT_LEN = 40  # minimum chars for cleaned content before falling back


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-clean existing page_content through the segmentation pipeline"
    )
    parser.add_argument("--limit", type=int, default=None, help="Only process first N rows")
    parser.add_argument("--domain", default=None, help="Only process rows for this domain_name")
    parser.add_argument("--id", type=int, default=None, dest="row_id", help="Only process one promo_website_id")
    parser.add_argument(
        "--source-type", default="markdown",
        help="Source type for process_page_content (default: markdown)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Report output directory")
    return parser.parse_args()


def fetch_staging_rows(
    client: SupabaseRestClient,
    args: argparse.Namespace,
) -> List[Dict[str, Any]]:
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
        offset += batch_limit
    return rows


def build_cleaned_content(processed: Dict[str, Any], original: str) -> str:
    """Build cleaned page_content from filtered segments (no [SEGMENT N] markers).

    Strategy:
    1. Extract text from `page_segments_filtered` → join with double newline
    2. Fall back to `page_content_llm` without markers if no filtered segments
    3. If result is too short but original has substantial content, keep original
    """
    # Prefer filtered segments
    segments = processed.get("page_segments_filtered") or []
    segment_texts = [s.get("text", "").strip() for s in segments if s.get("text", "").strip()]
    if segment_texts:
        cleaned = "\n\n".join(segment_texts)
        # If reasonable length, use it
        if len(cleaned) >= MIN_CONTENT_LEN:
            return cleaned

    # Fallback: page_content_llm without [SEGMENT N] markers
    import re

    llm = processed.get("page_content_llm", "").strip()
    if llm:
        no_markers = re.sub(r"\[SEGMENT\s+\d+\]\s*", "", llm, flags=re.IGNORECASE).strip()
        if len(no_markers) >= MIN_CONTENT_LEN:
            return no_markers

    # Fallback: raw_text from clean_page_text (already cleaned but not segmented)
    raw_text = processed.get("page_content", "").strip()
    if len(raw_text) >= MIN_CONTENT_LEN:
        return raw_text

    # Last resort: keep original
    return original


def main() -> None:
    args = parse_args()
    client = load_supabase_client()
    rows = fetch_staging_rows(client, args)

    if not rows:
        print(json.dumps({"status": "no_rows", "message": "No matching rows found"}))
        return

    output_rows: List[Dict[str, Any]] = []
    update_count = 0
    skip_count = 0
    error_count = 0

    for row in rows:
        pid = row.get("promo_website_id")
        url = (row.get("subpage_url") or "")[:80]
        old_content = (row.get("page_content") or "").strip()

        if not old_content:
            skip_count += 1
            output_rows.append({
                "promo_website_id": pid,
                "status": "skipped",
                "reason": "empty_content",
            })
            continue

        try:
            processed = process_page_content(old_content, source_type=args.source_type)
        except Exception as exc:
            error_count += 1
            output_rows.append({
                "promo_website_id": pid,
                "status": "error",
                "error": str(exc),
            })
            continue

        new_content = build_cleaned_content(processed, old_content)
        old_len = len(old_content)
        new_len = len(new_content)

        # Determine what kind of content we ended up with
        segments = processed.get("page_segments_filtered") or []
        from_filtered = bool(segments and len(new_content) < old_len)
        from_original = (new_content == old_content)
        content_type = (
            "filtered_segments" if from_filtered
            else "fallback_original" if from_original
            else "fallback_other"
        )

        if not args.dry_run and new_content != old_content:
            client.update_row(
                TABLE,
                {"promo_website_id": f"eq.{pid}"},
                {
                    "page_content": new_content,
                    "processed_status": False,
                },
            )
            update_count += 1

        output_rows.append({
            "promo_website_id": pid,
            "status": "updated" if (not args.dry_run and new_content != old_content) else "dry_run" if args.dry_run else "unchanged",
            "content_type": content_type,
            "old_len": old_len,
            "new_len": new_len,
            "changed": new_content != old_content,
            "subpage_url_preview": url,
        })

    now_iso = datetime.now(timezone.utc).isoformat()
    report_path = Path(args.output_dir) / f"{REPORT_PREFIX}_{now_iso.replace(':', '')}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    changed_count = sum(1 for r in output_rows if r.get("changed"))
    unchanged_count = sum(1 for r in output_rows if not r.get("changed") and r["status"] != "error" and r["status"] != "skipped")

    summary = {
        "status": "dry_run" if args.dry_run else "completed",
        "dry_run": bool(args.dry_run),
        "source_type": args.source_type,
        "total_rows": len(rows),
        "updated_rows": update_count,
        "changed_rows": changed_count,
        "unchanged_rows": unchanged_count,
        "skipped_rows": skip_count,
        "error_rows": error_count,
        "limit": args.limit,
        "domain": args.domain,
        "report_path": str(report_path),
    }
    report_path.write_text(
        json.dumps({**summary, "rows": output_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
