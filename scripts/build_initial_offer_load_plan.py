#!/usr/bin/env python3
"""Build dry-run initial offer load plans from promo_website_staging.

This script reads real staging rows, parses evidence segments, runs the existing
LLM extractor when configured, and writes local JSON/CSV artifacts describing
which promo_offer_master and promo_offer_evidence rows would be inserted.
It never writes to Supabase.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_evidence_segments import build_segment_records, summarize_segment_records
from utils.offer_initial_load import plan_initial_offer_load

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
TABLE = "promo_website_staging"
SELECT = "promo_website_id,subpage_url,domain_name,name,page_content,processed_status,crawl_timestamp,business_id,membership_context"
PAGE_SIZE = 1000


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
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
        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=90)
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
    parser = argparse.ArgumentParser(description="Dry-run initial offer load plan from staging evidence")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N staging rows")
    parser.add_argument("--domain", default=None, help="Filter by domain_name")
    parser.add_argument("--id", type=int, dest="row_id", default=None, help="Filter by promo_website_id")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Artifact output directory")
    parser.add_argument("--api-url", default=None, help="OpenAI-compatible chat completions URL")
    parser.add_argument("--model", default=None, help="LLM model name")
    parser.add_argument("--api-key-env", default="LLM_API_KEY", help="Env var holding the LLM API key")
    parser.add_argument("--no-llm", action="store_true", help="Do not call LLM; only segment and emit empty plans")
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
            SELECT,
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


def build_page_plan(row: Dict[str, Any], llm_client: Any, extractor: Optional[Any] = None) -> Dict[str, Any]:
    segment_records = build_segment_records(row)
    extraction: Dict[str, Any] = {
        "offers": [],
        "selected_segments": [],
        "candidate_block_selection": {"summary": "LLM disabled or unavailable."},
    }
    extract_error = ""
    if llm_client is not None:
        try:
            if extractor is None:
                from utils.offer_extraction_llm import extract_offers_for_row

                extractor = extract_offers_for_row
            llm_row = {
                **row,
                "page_segments_filtered": [
                    {
                        "index": item.get("segment_index"),
                        "tag": item.get("segment_type") or "text_block",
                        "text": item.get("text") or "",
                        "text_length": len(item.get("text") or ""),
                        "score": item.get("content_quality_score") or 0,
                    }
                    for item in segment_records
                    if item.get("is_offer_signal")
                ],
            }
            extraction = extractor(llm_row, client=llm_client)
        except Exception as exc:  # noqa: BLE001
            extract_error = str(exc)
    plan = plan_initial_offer_load(row, extraction.get("offers") or [], segment_records)
    summary = summarize_segment_records(segment_records)
    return {
        "promo_website_id": row.get("promo_website_id"),
        "domain_name": row.get("domain_name"),
        "name": row.get("name"),
        "source_url": row.get("subpage_url"),
        "segment_summary": summary,
        "extract_error": extract_error,
        "llm_selected_segments": extraction.get("selected_segments", []),
        "candidate_block_selection": extraction.get("candidate_block_selection", {}),
        "offers": extraction.get("offers", []),
        "plan": plan,
    }


def write_artifacts(output_dir: Path, pages: List[Dict[str, Any]]) -> Dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    json_path = output_dir / f"initial_offer_load_plan_{timestamp}.json"
    csv_path = output_dir / f"initial_offer_load_plan_{timestamp}_summary.csv"

    payload = {
        "summary": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "page_count": len(pages),
            "offers_extracted": sum(len(page.get("offers") or []) for page in pages),
            "master_rows": sum(len(page["plan"].get("master_rows") or []) for page in pages),
            "evidence_rows": sum(len(page["plan"].get("evidence_rows") or []) for page in pages),
            "extract_errors": sum(1 for page in pages if page.get("extract_error")),
        },
        "pages": pages,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "promo_website_id",
        "domain_name",
        "name",
        "source_url",
        "segment_count",
        "offer_signal_count",
        "offers_extracted",
        "master_rows",
        "evidence_rows",
        "duplicate_offers",
        "extract_error",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for page in pages:
            segment_summary = page.get("segment_summary") or {}
            plan_summary = (page.get("plan") or {}).get("summary") or {}
            writer.writerow(
                {
                    "promo_website_id": page.get("promo_website_id"),
                    "domain_name": page.get("domain_name"),
                    "name": page.get("name") or "",
                    "source_url": page.get("source_url"),
                    "segment_count": segment_summary.get("segment_count", 0),
                    "offer_signal_count": segment_summary.get("offer_signal_count", 0),
                    "offers_extracted": len(page.get("offers") or []),
                    "master_rows": plan_summary.get("master_rows", 0),
                    "evidence_rows": plan_summary.get("evidence_rows", 0),
                    "duplicate_offers": plan_summary.get("duplicate_offers", 0),
                    "extract_error": page.get("extract_error") or "",
                }
            )
    return {"json_path": str(json_path), "csv_path": str(csv_path)}


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    client = load_supabase_client()
    rows = fetch_staging_rows(client, args)
    llm_client = None
    if not args.no_llm:
        from utils.offer_extraction_llm import build_client_from_env

        llm_client = build_client_from_env(
            api_url=args.api_url,
            model=args.model,
            api_key_env=args.api_key_env,
        )
    pages = [build_page_plan(row, llm_client) for row in rows]
    paths = write_artifacts(Path(args.output_dir), pages)
    print(
        json.dumps(
            {
                "status": "dry_run",
                "llm_enabled": llm_client is not None,
                "pages": len(pages),
                "offers_extracted": sum(len(page.get("offers") or []) for page in pages),
                "master_rows": sum(len(page["plan"].get("master_rows") or []) for page in pages),
                "evidence_rows": sum(len(page["plan"].get("evidence_rows") or []) for page in pages),
                "report_paths": paths,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()