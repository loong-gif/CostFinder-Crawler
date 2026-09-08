#!/usr/bin/env python3
"""Create evidence-gated clinic-service repair operations for Supabase MCP.

The script deliberately does not use the repository's REST credentials: the
authoritative database is selected by the MCP caller.  Exported rows are input,
and the resulting SQL is executed through Supabase MCP after review.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.audit_missing_business_promotions_firecrawl import SelfHostedFirecrawl
from utils.clinic_service_quality import CANONICAL_SERVICES, evaluate_service, service_category, service_mentions
from utils.clinic_services_mcp_sql import render_mcp_operations

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_URL = "http://192.168.1.68:3002"
PAGE_TOKENS = ("pricing", "price", "service", "injectable", "membership", "special", "promotion")


def _host(url: str) -> str:
    return (urlparse(url if "://" in url else f"https://{url}").hostname or "").casefold().removeprefix("www.")


def _candidate_urls(website: str, source_url: str, mapped: list[Any]) -> list[str]:
    host = _host(website)
    values = [source_url] + [str(x.get("url") if isinstance(x, dict) else x) for x in mapped]
    seen: set[str] = set()
    selected: list[str] = []
    for url in values:
        url = str(url or "").strip().rstrip("/")
        if not url or _host(url) != host or url in seen:
            continue
        path = urlparse(url).path.casefold()
        if url == source_url.rstrip("/") or any(token in path for token in PAGE_TOKENS):
            selected.append(url)
            seen.add(url)
    return selected[:8]


def _source_evidence(row: dict[str, Any]) -> list[str]:
    return [str(row.get("service_raw_text") or "")]


def build_operations(rows: list[dict[str, Any]], businesses: dict[int, dict[str, Any]], *, crawl: bool, firecrawl: Any | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build deterministic updates and report rows; Firecrawl remains evidence-only."""
    operations: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    for row in rows:
        service = str(row.get("service_name") or "")
        if service not in CANONICAL_SERVICES:
            continue
        business_id = int(row["business_id"])
        evidence = _source_evidence(row)
        source_url = str(row.get("source_url") or "").strip()
        if crawl and firecrawl and businesses.get(business_id, {}).get("website"):
            try:
                website = str(businesses[business_id]["website"])
                mapped = firecrawl.map(website, limit=100)
                for url in _candidate_urls(website, source_url, mapped):
                    response = firecrawl.scrape(url)
                    data = response.get("data") if isinstance(response.get("data"), dict) else response
                    markdown = str(data.get("markdown") or "")
                    if markdown:
                        evidence.append(markdown)
                        if service in service_mentions(markdown):
                            source_url = url
                            break
            except Exception as exc:
                report.append({"service_id": row.get("service_id"), "reason": "firecrawl_failed", "error": str(exc)[:300]})
        decision = evaluate_service(service, evidence)
        raw = decision.raw_text or str(row.get("service_raw_text") or "")
        payload = {
            "business_id": business_id, "service_name": service,
            "service_name_raw": raw or None, "regular_price": float(decision.regular_price) if decision.regular_price is not None else None,
            "unit_type": decision.unit_type, "service_category": service_category(service),
            "service_area": row.get("service_area"), "source_url": source_url or None,
            "checked": decision.verified,
        }
        operations.append({"action": "mcp_update", "match": {"service_id": int(row["service_id"])}, "payload": payload})
        report.append({"service_id": row["service_id"], "business_id": business_id, "service_name": service, "old_regular_price": row.get("regular_price"), "new_regular_price": payload["regular_price"], "old_unit_type": row.get("unit_type"), "new_unit_type": decision.unit_type, "checked": decision.verified, "reason": decision.reason, "source_url": source_url, "raw_text": raw})
    return operations, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="JSON object with services and businesses arrays exported via MCP")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs"))
    parser.add_argument("--crawl", action="store_true")
    parser.add_argument("--firecrawl-api-url", default=DEFAULT_API_URL)
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows = list(payload.get("services") or [])
    businesses = {int(row["business_id"]): row for row in payload.get("businesses") or [] if row.get("business_id") is not None}
    firecrawl = SelfHostedFirecrawl(args.firecrawl_api_url, os.getenv("FIRECRAWL_API_KEY", "")) if args.crawl else None
    if firecrawl:
        firecrawl.healthcheck()
    operations, report = build_operations(rows, businesses, crawl=args.crawl, firecrawl=firecrawl)
    destination = Path(args.output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "clinic_services_quality_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "clinic_services_quality_repair.sql").write_text(render_mcp_operations(operations), encoding="utf-8")
    print(json.dumps({"operations": len(operations), "audit": str(destination / "clinic_services_quality_audit.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
