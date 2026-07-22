#!/usr/bin/env python3
"""Apply a pipeline bundle JSON to Supabase via REST (full raw payloads, no SQL excerpts)."""
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

from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key
from scripts.run_domain_architecture_pipeline import (
    build_actual_trace,
    link_item_services,
    schema_offer_to_master,
)
from utils.clinic_service_extraction import (
    is_promo_offer,
    offer_to_clinic_service_item,
    upsert_extracted_service,
)
from utils.firecrawl_scrape_raw_db import save_scrape_response, scrape_request_fingerprint, scrape_response_to_row_fields
from utils.scrape_markdown import prepare_scrape_markdown
from utils.firecrawl_search_raw_db import (
    save_search_queries,
    web_rows_from_search_file,
    web_rows_from_search_payload,
)
from utils.membership_plans import find_existing_plan_id
from utils.schema_contract import (
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_CLINIC_PROMOTIONS,
    TABLE_PROMO_OFFER_ITEMS,
    TABLE_PROMO_OFFER_MASTER,
)


def scrape_body(scrape: dict[str, Any]) -> dict[str, Any]:
    md = prepare_scrape_markdown(str(scrape.get("markdown") or ""))
    body: dict[str, Any] = {
        "markdown": md,
        "metadata": scrape.get("metadata"),
        "links": scrape.get("links"),
    }
    if scrape.get("scrape_job_id"):
        body["id"] = scrape["scrape_job_id"]
    if scrape.get("credits_used") is not None:
        body["creditsUsed"] = scrape["credits_used"]
    return {k: v for k, v in body.items() if v is not None}


def persist_memberships_from_bundle(
    client: Any,
    *,
    business_id: int,
    memberships: List[dict[str, Any]],
    source_url: str = "",
) -> List[int]:
    plan_ids: List[int] = []
    now = datetime.now(timezone.utc).isoformat()
    for item in memberships:
        name = str(item.get("membership_name") or "").strip()
        price = item.get("membership_price")
        if not name or price is None:
            continue
        existing = find_existing_plan_id(client, business_id, name)
        if existing:
            plan_ids.append(existing)
            continue
        benefits = item.get("benefits") or []
        row = {
            "business_id": business_id,
            "membership_name": name,
            "membership_price": float(price),
            "billing_period": item.get("billing_period") or "monthly",
            "benefits": [str(b).strip() for b in benefits if str(b).strip()],
            "created_at": now,
            "updated_at": now,
        }
        if item.get("minimum_commitment_months") is not None:
            row["minimum_commitment_months"] = item["minimum_commitment_months"]
        url = str(item.get("source_url") or source_url or "").strip().rstrip("/")
        if url:
            row["source_url"] = url
        inserted = client.insert_rows(TABLE_CLINIC_MEMBERSHIPS, [row])
        plan_ids.append(int(inserted[0]["plan_id"]))
    return plan_ids


def upsert_promotion_live(
    client: Any,
    *,
    business_id: int,
    source_url: str,
    promotion_title: str,
    promotion_description: str | None = None,
) -> int:
    norm = str(source_url or "").strip().rstrip("/")
    rows = client.fetch_rows(
        TABLE_CLINIC_PROMOTIONS,
        "promotion_id",
        filters={"source_url": f"eq.{norm}", "business_id": f"eq.{business_id}"},
        limit=1,
    )
    if rows:
        return int(rows[0]["promotion_id"])
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "business_id": business_id,
        "source_url": norm,
        "promotion_title": promotion_title,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    if promotion_description:
        payload["promotion_description"] = promotion_description
    inserted = client.insert_rows(TABLE_CLINIC_PROMOTIONS, [payload])
    return int(inserted[0]["promotion_id"])


def insert_offer_items_live(
    client: SupabaseRestClient,
    offer_id: int,
    items: List[dict[str, Any]],
) -> int:
    """Insert offer items using live promo_offer_items columns (no item_name)."""
    now = datetime.now(timezone.utc).isoformat()
    existing = client.fetch_rows(
        TABLE_PROMO_OFFER_ITEMS,
        "offer_item_id",
        filters={"offer_id": f"eq.{offer_id}"},
        limit=1,
    )
    if existing:
        return 0
    rows: List[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {
            "offer_id": offer_id,
            "created_at": now,
            "updated_at": now,
        }
        if item.get("service_id") is not None:
            row["service_id"] = item["service_id"]
        if item.get("quantity") is not None:
            row["quantity"] = item["quantity"]
        if item.get("unit_price") is not None:
            row["unit_price"] = item["unit_price"]
        rows.append(row)
    if not rows:
        rows = [{"offer_id": offer_id, "created_at": now, "updated_at": now}]
    client.insert_rows(TABLE_PROMO_OFFER_ITEMS, rows)
    return len(rows)


def persist_promotion_and_offers_from_bundle(
    client: Any,
    *,
    business_id: int,
    promotion: dict[str, Any] | None,
    offers: List[dict[str, Any]],
    membership_plan_ids: List[int],
) -> dict[str, int]:
    stats = {"promotions": 0, "offers": 0, "items": 0}
    if not promotion and not offers:
        return stats
    source_url = str((promotion or offers[0]).get("source_url") or "").strip().rstrip("/")
    if not source_url:
        return stats
    now = datetime.now(timezone.utc).isoformat()
    existing = client.fetch_rows(
        TABLE_CLINIC_PROMOTIONS,
        "promotion_id",
        filters={"business_id": f"eq.{business_id}", "source_url": f"eq.{source_url}"},
        limit=1,
    )
    if existing:
        promotion_id = int(existing[0]["promotion_id"])
    else:
        title = str((promotion or {}).get("promotion_title") or "Promotion").strip() or "Promotion"
        inserted = client.insert_rows(
            TABLE_CLINIC_PROMOTIONS,
            [
                {
                    "business_id": business_id,
                    "source_url": source_url,
                    "promotion_title": title,
                    "promotion_description": (promotion or {}).get("promotion_description"),
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
        promotion_id = int(inserted[0]["promotion_id"])
    stats["promotions"] = 1
    default_plan_id = membership_plan_ids[0] if membership_plan_ids else None
    for offer in offers:
        if not is_promo_offer(offer):
            service_item = offer_to_clinic_service_item(offer)
            if service_item:
                upsert_extracted_service(
                    client,
                    business_id=business_id,
                    item=service_item,
                    source_url=source_url,
                    evidence=str((promotion or {}).get("promotion_description") or ""),
                )
            continue
        master, items = schema_offer_to_master(
            offer,
            business_id=business_id,
            promotion_id=promotion_id,
            source_url=source_url,
            membership_plan_id=default_plan_id,
        )
        fp = master.get("offer_fingerprint")
        existing_offer = []
        if fp:
            existing_offer = client.fetch_rows(
                TABLE_PROMO_OFFER_MASTER,
                "id",
                filters={
                    "business_id": f"eq.{business_id}",
                    "offer_fingerprint": f"eq.{fp}",
                },
                limit=1,
            )
        if existing_offer:
            offer_id = int(existing_offer[0]["id"])
        else:
            inserted = client.insert_rows(TABLE_PROMO_OFFER_MASTER, [master])
            offer_id = int(inserted[0]["id"])
            stats["offers"] += 1
        item_count = insert_offer_items_live(client, offer_id, items)
        stats["items"] += item_count
    return stats


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = (os.getenv("SUPABASE_URL") or "").strip()
    key = get_supabase_secret_key()
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or service-role key")
    return SupabaseRestClient(base_url, key)


def load_loulou_scrape_from_disk(source_url: str) -> dict[str, Any] | None:
    """Prefer fresh Firecrawl CLI captures with --only-main-content."""
    fc = PROJECT_ROOT / ".firecrawl/louloumedspa"
    url = str(source_url or "").rstrip("/").lower()
    mapping = {
        "https://louloumedspa.com/membership": fc / "scrape-membership.json",
        "https://louloumedspa.com/services/botox-dysport-and-daxxify-in-oklahoma-city-ok": fc
        / "scrape-botox-service.json",
    }
    path = mapping.get(url)
    if not path or not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    fields = scrape_response_to_row_fields(payload)
    if not fields.get("markdown"):
        return None
    return {
        "source_url": source_url,
        "markdown": fields["markdown"],
        "metadata": fields.get("metadata"),
        "links": fields.get("links"),
    }


LOULOU_SEARCH_SPECS = (
    ("search-membership.json", "site:louloumedspa.com membership"),
    ("search-botox.json", "site:louloumedspa.com botox pricing"),
    ("search-specials.json", "site:louloumedspa.com specials promotions"),
)


def iter_search_entries(bundle: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    domain = str(bundle.get("domain") or "")
    sr = bundle.get("search_raw") or {}
    if domain == "louloumedspa.com":
        fc = PROJECT_ROOT / ".firecrawl/louloumedspa"
        entries: list[tuple[str, list[dict[str, Any]]]] = []
        for filename, query in LOULOU_SEARCH_SPECS:
            path = fc / filename
            if path.exists():
                entries.append((query, web_rows_from_search_file(path)))
        if entries:
            return entries
    queries: list[str] = []
    if isinstance(sr.get("response_json"), dict):
        raw_queries = sr["response_json"].get("queries")
        if isinstance(raw_queries, list):
            queries = [str(q).strip() for q in raw_queries if str(q).strip()]
    if not queries:
        combined = str(sr.get("search_query") or "").strip()
        if combined:
            queries = [part.strip() for part in combined.split("|") if part.strip()]
    if len(queries) <= 1:
        rows = web_rows_from_search_payload(sr.get("response_json"))
        query = queries[0] if queries else str(sr.get("search_query") or "").strip()
        return [(query, rows)] if query and rows else []
    return [(query, web_rows_from_search_payload(sr.get("response_json"))) for query in queries]


def pick_search_raw_id_for_scrape(source_url: str, search_raw_ids: dict[str, int]) -> int:
    url = str(source_url or "").lower()
    if "membership" in url:
        for query, row_id in search_raw_ids.items():
            if "membership" in query.lower():
                return row_id
    if any(token in url for token in ("botox", "dysport", "daxxify", "tox", "neurotoxin")):
        for query, row_id in search_raw_ids.items():
            q = query.lower()
            if any(token in q for token in ("botox", "dysport", "daxxify", "injectable", "tox")):
                return row_id
    if any(token in url for token in ("special", "promo", "offer", "deal")):
        for query, row_id in search_raw_ids.items():
            if any(token in query.lower() for token in ("special", "promo", "offer", "deal")):
                return row_id
    return next(iter(search_raw_ids.values()))


def apply_bundle(bundle_path: Path, *, trace_out: Path | None = None) -> dict[str, Any]:
    os.environ.setdefault("ALLOW_SERVICE_ROLE_WRITES", "true")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    business_id = int(bundle["business_id"])
    domain = str(bundle.get("domain") or "louloumedspa.com")
    client = load_client()
    website = str(bundle.get("website") or domain)

    search_entries = iter_search_entries(bundle)
    if not search_entries:
        raise RuntimeError("No search entries found in bundle")
    search_raw_ids = save_search_queries(
        client,
        website=website,
        domain=domain,
        entries=search_entries,
        success=True,
    )
    membership_query = next((q for q in search_raw_ids if "membership" in q.lower()), None)
    search_raw_id = search_raw_ids.get(membership_query or next(iter(search_raw_ids)))

    scrape_rows: List[dict[str, Any]] = []
    for scrape in bundle.get("scrapes") or []:
        disk = load_loulou_scrape_from_disk(str(scrape.get("source_url") or ""))
        if disk:
            scrape = {**scrape, **disk}
        md = prepare_scrape_markdown(str(scrape.get("markdown") or ""))
        scrape = {**scrape, "markdown": md}
        rows = save_scrape_response(
            client,
            scrape_request_fingerprint(str(scrape["source_url"]), only_main_content=True),
            str(scrape["source_url"]),
            scrape_body(scrape),
            search_raw_id=pick_search_raw_id_for_scrape(scrape["source_url"], search_raw_ids),
            success=True,
        )
        row = rows[0]
        scrape_rows.append(
            {
                "id": int(row["id"]),
                "source_url": scrape["source_url"],
                "markdown": md,
            }
        )

    membership_url = next(
        (
            str(scrape.get("source_url") or "").strip().rstrip("/")
            for scrape in bundle.get("scrapes") or []
            if "membership" in str(scrape.get("source_url") or "").lower()
        ),
        "",
    )
    plan_ids = persist_memberships_from_bundle(
        client,
        business_id=business_id,
        memberships=bundle.get("memberships") or [],
        source_url=membership_url,
    )
    offer_stats = persist_promotion_and_offers_from_bundle(
        client,
        business_id=business_id,
        promotion=bundle.get("promotion"),
        offers=bundle.get("offers") or [],
        membership_plan_ids=plan_ids,
    )
    try:
        linked = link_item_services(client, business_id=business_id)
    except Exception:
        linked = 0

    trace = build_actual_trace(
        domain=domain,
        business_id=business_id,
        search_raw_id=search_raw_id,
        scrape_rows=scrape_rows,
        membership_plan_ids=plan_ids,
        service_ids=[],
        offer_stats=offer_stats,
    )
    trace["linked_item_services"] = linked
    trace["search_raw_ids"] = search_raw_ids
    trace["skipped"] = {
        "clinic_services": [
            s.get("service_name")
            for s in (bundle.get("services") or [])
            if s.get("regular_price") is None
        ]
    }

    if trace_out:
        trace_out.parent.mkdir(parents=True, exist_ok=True)
        trace_out.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    return trace


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply pipeline bundle to Supabase")
    parser.add_argument(
        "--bundle",
        type=Path,
        default=PROJECT_ROOT / "output/results/louloumedspa_pipeline_bundle.json",
    )
    parser.add_argument(
        "--trace-out",
        type=Path,
        default=PROJECT_ROOT
        / ".cursor/skills/costfinder-architecture/examples/louloumedspa.com.actual.trace.json",
    )
    args = parser.parse_args()
    trace = apply_bundle(args.bundle, trace_out=args.trace_out)
    print(json.dumps(trace, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
