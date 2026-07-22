#!/usr/bin/env python3
"""Scrape one pricing page → clinic_services + promo_offer_master (+ promotion anchor)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.clinic_promotions_db import upsert_promotion
from utils.clinic_service_extraction import is_promo_offer
from utils.firecrawl_client import get_firecrawl_client, scrape_page_markdown
from utils.firecrawl_scrape_raw_db import save_scrape_response, scrape_request_fingerprint
from utils.offer_extraction_llm import build_client_from_env
from utils.extraction_persist import route_and_persist_extraction
from utils.schema_contract import TABLE_PROMO_OFFER_MASTER
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key

from scripts.run_domain_architecture_pipeline import (
    link_item_services,
    llm_extract,
    load_schema,
)


from contextlib import contextmanager


_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")


def _strip_proxy_env() -> None:
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)


@contextmanager
def _without_http_proxy():
    saved = {key: os.environ.pop(key, None) for key in _PROXY_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def page_title_from_scrape(body: dict[str, Any], fallback: str) -> str:
    meta = body.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    for key in ("title", "ogTitle", "og:title"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return fallback


def scrape_and_persist(client: SupabaseRestClient, url: str) -> dict[str, Any]:
    with _without_http_proxy():
        fc = get_firecrawl_client()
        # get_firecrawl_client → load_dotenv re-injects proxy from .env; Firecrawl must go direct.
        _strip_proxy_env()
        markdown, body = scrape_page_markdown(fc, url)
    fp = scrape_request_fingerprint(url, only_main_content=True)
    rows = save_scrape_response(client, fp, url, body, success=True)
    row = rows[0]
    return {
        "scrape_raw_id": int(row["id"]),
        "source_url": str(row.get("source_url") or url).rstrip("/"),
        "markdown": markdown,
        "metadata": row.get("metadata") or body.get("metadata"),
    }


def extract_offers(
    client: SupabaseRestClient,
    *,
    business_id: int,
    promotion_id: int,
    source_url: str,
    markdown: str,
    llm: Any,
    apply: bool,
) -> dict[str, Any]:
    schema = load_schema("offer_extraction_schema.json")
    payload = llm_extract(
        llm,
        schema,
        task=(
            "Extract every concrete purchasable price line on this pricing menu. "
            "Output one offer per distinct price line. "
            "Set regular_price for standard menu/list prices; leave discount_price, discount_percent, and discount_amount null. "
            "Fill any one of discount_price / discount_percent / discount_amount for explicit specials only. "
            "Set items[].service_name to the standardized treatment (e.g. Dysport, Jeuveau, Dermal Filler)."
        ),
        source_url=source_url,
        markdown=markdown,
    )
    offers = payload.get("offers") or []
    stats: dict[str, Any] = {
        "offers": 0,
        "items": 0,
        "skipped": 0,
        "services_from_offers": 0,
        "offer_ids": [],
        "service_writes": [],
    }
    if not apply:
        stats["dry_run_offers"] = len(offers)
        stats["dry_run_promo_offers"] = sum(1 for offer in offers if is_promo_offer(offer))
        stats["dry_run_service_offers"] = sum(1 for offer in offers if not is_promo_offer(offer))
        return stats

    existing = client.fetch_rows(
        TABLE_PROMO_OFFER_MASTER,
        "id,offer_fingerprint",
        filters={"business_id": f"eq.{business_id}", "promotion_id": f"eq.{promotion_id}"},
        limit=500,
    )
    seen = {str(row.get("offer_fingerprint") or "") for row in existing if row.get("offer_fingerprint")}
    routed = route_and_persist_extraction(
        client,
        business_id=business_id,
        promotion_id=promotion_id,
        source_url=source_url,
        offers=offers,
        evidence=markdown,
        seen_fingerprints=seen,
    )
    stats.update(
        {
            "offers": routed["promos"],
            "items": sum(write.get("items", 0) for write in routed["promo_writes"] if write.get("accepted")),
            "skipped": routed["skipped"],
            "services_from_offers": routed["services"],
            "service_writes": routed["service_writes"],
            "quarantined": routed["quarantined"],
        }
    )
    stats["offer_ids"] = [
        write["offer_id"] for write in routed["promo_writes"] if write.get("offer_id")
    ]
    return stats


def run(
    *,
    client: SupabaseRestClient,
    llm: Any,
    business_id: int,
    url: str,
    apply: bool,
) -> dict[str, Any]:
    scrape = scrape_and_persist(client, url)
    source_url = scrape["source_url"]
    markdown = str(scrape["markdown"])
    title = page_title_from_scrape(
        {"metadata": scrape.get("metadata")},
        "Services & Pricing",
    )
    audit: dict[str, Any] = {
        "business_id": business_id,
        "source_url": source_url,
        "scrape_raw_id": scrape["scrape_raw_id"],
        "promotion_title": title,
        "apply": apply,
        "markdown_chars": len(markdown),
    }
    if not apply:
        audit["offers"] = extract_offers(
            client,
            business_id=business_id,
            promotion_id=0,
            source_url=source_url,
            markdown=markdown,
            llm=llm,
            apply=False,
        )
        return audit

    promotion_id = upsert_promotion(
        client,
        business_id=business_id,
        source_url=source_url,
        promotion_title=title,
    )
    client.update_row(
        "clinic_promotions",
        {"promotion_id": f"eq.{promotion_id}"},
        {
            "promotion_title": title,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    offer_stats = extract_offers(
        client,
        business_id=business_id,
        promotion_id=promotion_id,
        source_url=source_url,
        markdown=markdown,
        llm=llm,
        apply=True,
    )
    linked = link_item_services(client, business_id=business_id)
    audit.update(
        {
            "promotion_id": promotion_id,
            "offer_stats": offer_stats,
            "linked_item_services": linked,
        }
    )
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", type=int, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    llm = build_client_from_env()
    if llm is None:
        raise RuntimeError("LLM not configured")
    client = SupabaseRestClient(os.getenv("SUPABASE_URL", "").strip(), get_supabase_secret_key())
    audit = run(
        client=client,
        llm=llm,
        business_id=args.business_id,
        url=args.url.strip(),
        apply=args.apply,
    )
    out = PROJECT_ROOT / ".firecrawl/master-business-search/scrape-page-pipeline-audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
