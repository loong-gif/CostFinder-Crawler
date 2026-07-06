#!/usr/bin/env python3
"""
Extract offers via LLM for unmatched promo_website_staging URLs and insert into promo_offer_master.

输入：detect_promo_website_staging_changes.py 产出的 classified 报告 JSON。
默认只处理 unmatched_reason=no_offers_on_domain 的 changed 行（整域从未入库的页面）。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
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

from crawler.staging_recrawl import SupabaseRestClient
from utils.change_driven_extractor import build_offer_update_payload
from utils.offer_extraction_llm import build_client_from_env, extract_offers_for_row
from utils.page_content_processor import process_page_content

OFFER_TABLE = "promo_offer_master"
REPORT_PREFIX = "extract_offers_unmatched"
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
DEFAULT_REASON_FILTER = "no_offers_on_domain"
DEFAULT_LLM_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_LLM_MODEL = "gpt-5-mini"
DEFAULT_LLM_API_KEY_ENV = "OPENAI_API_KEY"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LLM-extract offers for unmatched staging URLs and insert into promo_offer_master")
    parser.add_argument("--from-report", required=True, help="detect_promo_website_staging_change_detect_*.json path")
    parser.add_argument("--reason-filter", default=DEFAULT_REASON_FILTER, help=f"Only extract rows with this unmatched_reason (default: {DEFAULT_REASON_FILTER}); pass '' to extract all unmatched")
    parser.add_argument("--only-ids", default=None, help="Comma-separated promo_website_id list to restrict processing (overrides reason filter)")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N unmatched rows")
    parser.add_argument("--concurrency", type=int, default=5, help="Firecrawl fetch concurrency (default: 5)")
    parser.add_argument("--extract-concurrency", type=int, default=3, help="LLM extract+insert thread concurrency (default: 3)")
    parser.add_argument("--api-url", default=DEFAULT_LLM_API_URL, help="OpenAI-compatible chat completions URL")
    parser.add_argument("--model", default=DEFAULT_LLM_MODEL, help="LLM model name")
    parser.add_argument("--api-key-env", default=DEFAULT_LLM_API_KEY_ENV, help="Env var holding the LLM API key")
    parser.add_argument("--dry-run", action="store_true", help="Extract via LLM but do NOT insert into promo_offer_master")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Report output directory")
    return parser.parse_args()


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def load_unmatched_rows(report_path: str, reason_filter: str, limit: Optional[int], only_ids: Optional[set]) -> List[Dict[str, Any]]:
    payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    results = payload.get("results") or []
    rows = []
    for r in results:
        if r.get("change_type") != "changed" or r.get("linked_offer_count"):
            continue
        if only_ids is not None:
            if r.get("promo_website_id") not in only_ids:
                continue
        elif reason_filter and r.get("unmatched_reason") != reason_filter:
            continue
        rows.append(r)
    if limit:
        rows = rows[:limit]
    return rows


async def fetch_page_content_firecrawl(url: str) -> Dict[str, Any]:
    from crawler.fetch_engine import FirecrawlFetchEngine

    engine = FirecrawlFetchEngine()
    try:
        page = await engine.fetch(url)
        processed = process_page_content(page.content or "", source_type="markdown")
        page_content = processed.get("page_content") or processed.get("page_content_llm") or ""
        if not str(page_content).strip():
            return {"success": False, "error_message": "empty_content_after_processing", "processed": processed}
        return {"success": True, "page_content": page_content, "processed": processed, "error_message": ""}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error_message": str(exc), "processed": {}}


async def recrawl_unmatched(rows: List[Dict[str, Any]], concurrency: int) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(row: Dict[str, Any]) -> Dict[str, Any]:
        url = str(row.get("subpage_url") or "").strip()
        async with sem:
            crawl = await fetch_page_content_firecrawl(url)
        return {**row, "recrawl": crawl}

    return list(await asyncio.gather(*(one(r) for r in rows)))


def build_insert_payload(offer: Dict[str, Any], *, source_url: str, source_name: str) -> Dict[str, Any]:
    payload = build_offer_update_payload(offer)
    payload.update(
        {
            "channel": "Website",
            "status": "active",
            "source_url": source_url,
            "source_name": source_name,
            "moderation_status": "approved",
        }
    )
    return payload


def _extract_and_insert_one(
    row: Dict[str, Any],
    llm_client: Any,
    sb_client: SupabaseRestClient,
    dry_run: bool,
) -> Dict[str, Any]:
    url = str(row.get("subpage_url") or "").strip()
    domain = str(row.get("domain_name") or "").strip()
    recrawl = row.get("recrawl") or {}
    result: Dict[str, Any] = {
        "promo_website_id": row.get("promo_website_id"),
        "subpage_url": url,
        "domain_name": domain,
        "unmatched_reason": row.get("unmatched_reason"),
        "recrawl_success": recrawl.get("success"),
        "recrawl_error": recrawl.get("error_message", ""),
        "extracted_offers": 0,
        "inserted": 0,
        "insert_errors": [],
        "offers_preview": [],
    }
    if not recrawl.get("success"):
        return result

    page_content = recrawl.get("page_content") or ""
    llm_row = {"page_content": page_content, "domain_name": domain, "subpage_url": url}
    try:
        extraction = extract_offers_for_row(llm_row, client=llm_client)
    except Exception as exc:  # noqa: BLE001
        result["extract_error"] = str(exc)
        return result

    offers = extraction.get("offers") or []
    result["extracted_offers"] = len(offers)
    result["offers_preview"] = [
        {"service_name": o.get("service_name"), "offer_raw_text": str(o.get("offer_raw_text") or "")[:120]}
        for o in offers[:5]
    ]

    for offer in offers:
        payload = build_insert_payload(offer, source_url=url, source_name=domain)
        if not payload.get("service_name") and not payload.get("offer_raw_text"):
            continue
        if dry_run:
            result["inserted"] += 1
            continue
        try:
            sb_client.insert_rows(OFFER_TABLE, [payload])
            result["inserted"] += 1
        except Exception as exc:  # noqa: BLE001
            result["insert_errors"].append(str(exc))
    return result


def extract_and_insert(
    client: SupabaseRestClient,
    rows: List[Dict[str, Any]],
    llm_client: Any,
    *,
    dry_run: bool,
    concurrency: int = 3,
) -> List[Dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor

    # ponytail: LLM 调用同步阻塞，线程池并发 extract+insert；PostgREST 单条 insert 并发安全。
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return list(
            pool.map(lambda r: _extract_and_insert_one(r, llm_client, client, dry_run), rows)
        )


def write_reports(output_dir: Path, rows: List[Dict[str, Any]], *, mode: str, reason_filter: str) -> Dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"{REPORT_PREFIX}_{ts}.json"
    csv_path = output_dir / f"{REPORT_PREFIX}_{ts}.csv"

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "reason_filter": reason_filter,
        "total": len(rows),
        "results": rows,
        "summary": {
            "recrawl_success": sum(1 for r in rows if r.get("recrawl_success")),
            "extracted_offers_total": sum(int(r.get("extracted_offers") or 0) for r in rows),
            "inserted_total": sum(int(r.get("inserted") or 0) for r in rows),
            "extract_errors": sum(1 for r in rows if r.get("extract_error")),
            "insert_errors_total": sum(len(r.get("insert_errors") or []) for r in rows),
        },
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "promo_website_id",
        "subpage_url",
        "domain_name",
        "unmatched_reason",
        "recrawl_success",
        "extracted_offers",
        "inserted",
        "recrawl_error",
        "extract_error",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return {"json_path": str(json_path), "csv_path": str(csv_path)}


def main() -> None:
    args = parse_args()
    sb_client = load_supabase_client()
    llm_client = build_client_from_env(api_url=args.api_url, model=args.model, api_key_env=args.api_key_env)
    if llm_client is None:
        raise RuntimeError(
            f"LLM client not configured: api_url={args.api_url} model={args.model} api_key_env={args.api_key_env}"
        )

    only_ids = None
    if args.only_ids:
        only_ids = {int(x.strip()) for x in args.only_ids.split(",") if x.strip().isdigit()}
    rows = load_unmatched_rows(args.from_report, args.reason_filter, args.limit, only_ids)
    if not rows:
        print(json.dumps({"status": "no_rows", "message": "No unmatched rows matched filter"}, ensure_ascii=False))
        return

    rows = asyncio.run(recrawl_unmatched(rows, args.concurrency))
    rows = extract_and_insert(sb_client, rows, llm_client, dry_run=args.dry_run, concurrency=args.extract_concurrency)
    mode = "dry_run" if args.dry_run else "apply"
    paths = write_reports(Path(args.output_dir), rows, mode=mode, reason_filter=args.reason_filter)

    print(
        json.dumps(
            {
                "status": mode,
                "total": len(rows),
                "reason_filter": args.reason_filter,
                "summary": {
                    "recrawl_success": sum(1 for r in rows if r.get("recrawl_success")),
                    "extracted_offers_total": sum(int(r.get("extracted_offers") or 0) for r in rows),
                    "inserted_total": sum(int(r.get("inserted") or 0) for r in rows),
                },
                "report_paths": paths,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
