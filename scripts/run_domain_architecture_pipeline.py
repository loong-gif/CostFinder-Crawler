#!/usr/bin/env python3
"""Run CostFinder architecture pipeline for one domain: Search → raw → Scrape → extract → DB.

Example:
  python scripts/run_domain_architecture_pipeline.py --domain louloumedspa.com --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_DIR
from utils.supabase_access import load_supabase_client
from utils.clinic_services_search import business_base_domain
from utils.domain_pipeline_persist import (
    link_item_services,
    persist_memberships,
    persist_promotions_and_offers,
    persist_services,
)
from utils.domain_pipeline_report import build_actual_trace, resolve_business
from utils.domain_pipeline_search import (
    DOMAIN_QUERIES,
    PROMO_PATH_RE,
    llm_extract,
    load_schema,
    persist_search_queries,
    pick_primary_search_raw_id,
    pick_scrape_urls,
    scrape_and_persist,
    search_domain,
)
from utils.firecrawl_client import get_firecrawl_client, scrape_page_markdown
from utils.firecrawl_scrape_raw_db import scrape_request_fingerprint
from utils.membership_paths import is_membership_page_url
from utils.offer_extraction_llm import build_client_from_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Run architecture pipeline for one domain.")
    parser.add_argument("--domain", default="louloumedspa.com")
    parser.add_argument("--business-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true", help="Write to Supabase (default dry-run crawl only)")
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="Run Search/Scrape/LLM and write report JSON only (no Supabase REST)",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--visibility-manifest",
        type=Path,
        default=None,
        help="Rendered visibility report produced by the ego browser gate; required with --apply",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    client = load_supabase_client() if args.apply else None
    if client is not None:
        business = resolve_business(client, domain=args.domain, business_id=args.business_id)
    elif args.business_id is not None:
        business = {
            "business_id": args.business_id,
            "name": args.domain,
            "website": args.domain,
        }
    else:
        raise RuntimeError("Use --business-id when Supabase writer key is unavailable")
    business_id = int(business["business_id"])
    website = business.get("website") or args.domain
    domain = business_base_domain(website) or args.domain

    all_queries = [q for group in DOMAIN_QUERIES.values() for q in group]
    pages, query_pages = search_domain(website, all_queries)
    scrape_urls = pick_scrape_urls(pages, domain)

    report: Dict[str, Any] = {
        "domain": domain,
        "business_id": business_id,
        "business_name": business.get("name"),
        "search_pages": len(pages),
        "search_urls": [p.url for p in pages],
        "scrape_urls": scrape_urls,
        "apply": args.apply,
    }

    if not args.apply and not args.extract_only:
        report["status"] = "dry_run"
        out = args.output or (OUTPUT_DIR / f"pipeline_{domain.replace('.', '_')}_dryrun.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"report={out}")
        return

    if args.apply and args.visibility_manifest is None:
        raise RuntimeError("--visibility-manifest is required with --apply")

    rendered_visibility: dict[str, dict[str, Any]] = {}
    if args.visibility_manifest is not None:
        visibility_payload = json.loads(args.visibility_manifest.read_text(encoding="utf-8"))
        rendered_visibility = visibility_payload.get("results", visibility_payload)

    llm = build_client_from_env()
    if llm is None:
        raise RuntimeError("LLM client not configured (LLM_API_KEY / LLM_MODEL)")

    membership_schema = load_schema("membership_extraction_schema.json")
    service_schema = load_schema("clinic_services_firecrawl_extraction_schema.json")
    promo_schema = load_schema("promotion_extraction_schema.json")
    offer_schema = load_schema("offer_extraction_schema.json")

    membership_pages = [p for p in pages if is_membership_page_url(p.url)] or list(pages[:1])
    service_pages = [
        p
        for p in pages
        if not is_membership_page_url(p.url) and not PROMO_PATH_RE.search(urlparse(p.url).path or "")
    ] or list(pages)

    membership_extracts = [
        llm_extract(
            llm,
            membership_schema,
            task="Extract purchasable membership plans with explicit prices.",
            source_url=p.url,
            markdown=p.markdown,
        )
        for p in membership_pages[:3]
    ]
    service_extracts = [
        llm_extract(
            llm,
            service_schema,
            task="Extract explicit regular (non-promotional) service unit prices only.",
            source_url=p.url,
            markdown=p.markdown,
        )
        for p in service_pages[:4]
    ]

    scrape_payloads: List[Dict[str, Any]] = []
    fc = get_firecrawl_client()
    for url in scrape_urls:
        try:
            md, body = scrape_page_markdown(fc, url)
            promo = llm_extract(
                llm,
                promo_schema,
                task="Extract concrete clinic promotions; empty array if none.",
                source_url=url,
                markdown=md,
            )
            offers = {"offers": []}
            if promo.get("promotions"):
                offers = llm_extract(
                    llm,
                    offer_schema,
                    task=(
                "Extract every concrete purchasable offer on this promotion page. "
                "OR alternatives (or/and/,/或, one shared price/discount): purchase_type=independent_options or split singles, is_package=false, one item_name per option. "
                "AND bundle (one total for multiple treatments): purchase_type=bundle, is_package=true, price_model=total, separate items[] per component, item unit_price=null."
            ),
                    source_url=url,
                    markdown=md,
                )
            scrape_payloads.append(
                {
                    "source_url": url,
                    "request_fingerprint": scrape_request_fingerprint(url, only_main_content=True),
                    "response_json": body,
                    "markdown": md,
                    "promotion": promo,
                    "offers": offers,
                }
            )
        except Exception as exc:
            scrape_payloads.append(
                {
                    "source_url": url,
                    "request_fingerprint": scrape_request_fingerprint(url, only_main_content=True),
                    "error": str(exc),
                }
            )

    bundle = {
        "domain": domain,
        "website": website,
        "search_raw": {
            "queries": all_queries,
            "response_json": {
                "pages": [{"url": p.url, "markdown": p.markdown, "title": p.title} for p in pages],
                "queries": all_queries,
            },
        },
        "membership_extracts": membership_extracts,
        "service_extracts": service_extracts,
        "scrapes": scrape_payloads,
    }
    report["bundle"] = bundle

    if args.extract_only:
        report["status"] = "extract_only"
        out = args.output or (OUTPUT_DIR / f"pipeline_{domain.replace('.', '_')}_extract.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "bundle"}, ensure_ascii=False, indent=2))
        print(f"report={out}")
        return

    search_raw_ids = persist_search_queries(
        client, website=website, domain=domain, query_pages=query_pages
    )
    search_raw_id = pick_primary_search_raw_id(search_raw_ids)
    scrapes = scrape_and_persist(client, scrape_urls, search_raw_ids=search_raw_ids)

    membership_schema = load_schema("membership_extraction_schema.json")
    service_schema = load_schema("clinic_services_firecrawl_extraction_schema.json")
    promo_schema = load_schema("promotion_extraction_schema.json")
    offer_schema = load_schema("offer_extraction_schema.json")

    plan_ids = persist_memberships(
        client, business_id=business_id, pages=pages, llm=llm, schema=membership_schema, rendered_visibility=rendered_visibility
    )
    service_ids = persist_services(
        client,
        business_id=business_id,
        pages=pages,
        llm=llm,
        schema=service_schema,
        domain=domain,
    )
    offer_stats = persist_promotions_and_offers(
        client,
        business_id=business_id,
        scrapes=scrapes,
        llm=llm,
        promo_schema=promo_schema,
        offer_schema=offer_schema,
        membership_plan_ids=plan_ids,
        rendered_visibility=rendered_visibility,
    )
    linked = link_item_services(client, business_id=business_id)

    trace = build_actual_trace(
        domain=domain,
        business_id=business_id,
        search_raw_id=search_raw_id,
        scrape_rows=scrapes,
        membership_plan_ids=plan_ids,
        service_ids=service_ids,
        offer_stats=offer_stats,
    )
    trace["linked_item_services"] = linked

    trace_path = (
        PROJECT_ROOT
        / ".cursor/skills/costfinder-architecture/examples/louloumedspa.com.actual.trace.json"
    )
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")

    report.update(
        {
            "status": "ok",
            "search_raw_id": search_raw_id,
            "search_raw_ids": search_raw_ids,
            "scrape_ids": [s["id"] for s in scrapes],
            "membership_plan_ids": plan_ids,
            "service_ids": service_ids,
            "offer_stats": offer_stats,
            "linked_item_services": linked,
            "trace_path": str(trace_path),
        }
    )
    out = args.output or (OUTPUT_DIR / f"pipeline_{domain.replace('.', '_')}_{datetime.now():%Y%m%d_%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={out}")
    print(f"trace={trace_path}")


if __name__ == "__main__":
    main()
