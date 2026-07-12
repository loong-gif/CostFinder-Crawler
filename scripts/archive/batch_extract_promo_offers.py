#!/usr/bin/env python3
"""
Batch promo offer extraction from promo_website_staging → promo_offer_master.

Follows the PRD pipeline:
  Step 1: Data cleaning (404 filter, dedup, short content, field completion)
  Step 2: Content sectioning → handled by extract_offers_for_row() internally
  Step 3: Structured extraction via LLM → handled by extract_offers_for_row()
  Step 4: Insert + QA checks

Usage (--dry-run to preview without writing to DB):
  python scripts/batch_extract_promo_offers.py --dry-run
  python scripts/batch_extract_promo_offers.py --limit 20
  python scripts/batch_extract_promo_offers.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_extraction_llm import build_client_from_env, extract_offers_for_row, normalize_offer_record
from utils.supabase_rest import SupabaseRestClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
REPORT_DIR = PROJECT_ROOT / "reports"
STAGING_TABLE = "promo_website_staging"
OFFER_TABLE = "promo_offer_master"
MAX_PAGE_CONTENT_CHARS = 100000  # truncate to avoid LLM token blowup

# PRD Step 1: 404 patterns
ERROR_404_PATTERNS = [
    re.compile(r"404\s*==="),
    re.compile(r"page could not be found", re.IGNORECASE),
    re.compile(r"oops.*not be found", re.IGNORECASE),
]

# PRD Step 2: OFFER_SIGNALS
OFFER_SIGNALS = [
    re.compile(r"\$\d"),
    re.compile(r"\d+%\s*(?:off|OFF)"),
    re.compile(r"[Bb]uy\s+\d"),
    re.compile(r"[Ff]ree"),
    re.compile(r"[Ss]ave\s+\$?\d"),
    re.compile(r"[Ss]tarting\s+at"),
    re.compile(r"\d+\s*(?:per|/)\s*(?:unit|month|session|vial|syringe|area)"),
    re.compile(r"[Mm]embership"),
    re.compile(r"[Ss]pecial"),
    re.compile(r"[Pp]ackage"),
]
# Rescued by price even if noise
PRICE_PATTERN = re.compile(r"\$\d")

# ---------------------------------------------------------------------------
# Step 1: Data Cleaning
# ---------------------------------------------------------------------------

def _is_error_404(content: str) -> bool:
    return any(p.search(content) for p in ERROR_404_PATTERNS)


def _content_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def clean_staging_rows(rows: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Step 1: Filter and clean rows. Returns (clean, skip_log)."""
    skip_log: List[Dict[str, Any]] = []

    # --- Rule 1: Filter 404 ---
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        content = row.get("page_content", "") or ""
        if _is_error_404(content):
            skip_log.append({"promo_website_id": row["promo_website_id"], "skip_reason": "error_404", "subpage_url": row.get("subpage_url", "")})
        else:
            filtered.append(row)

    # --- Rule 2: Filter short content (< 500 chars and no $ sign) ---
    cleaned: List[Dict[str, Any]] = []
    for row in filtered:
        content = row.get("page_content", "") or ""
        if len(content) < 500 and "$" not in content:
            skip_log.append({"promo_website_id": row["promo_website_id"], "skip_reason": "content_too_short", "subpage_url": row.get("subpage_url", "")})
        else:
            cleaned.append(row)

    # --- Rule 3: Dedup by (domain_name, content_hash) ---
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in cleaned:
        key = f"{row.get('domain_name', '')}::{_content_hash(row.get('page_content', '') or '')}"
        groups.setdefault(key, []).append(row)

    deduped: List[Dict[str, Any]] = []
    for key, group in groups.items():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            # Keep latest crawl_timestamp
            group.sort(key=lambda r: r.get("crawl_timestamp", ""), reverse=True)
            deduped.append(group[0])
            for dup in group[1:]:
                skip_log.append({"promo_website_id": dup["promo_website_id"], "skip_reason": "duplicate_content", "subpage_url": dup.get("subpage_url", "")})

    # --- Rule 4: Fill missing name with domain ---
    for row in deduped:
        name = (row.get("name") or "").strip()
        if not name:
            domain = row.get("domain_name", "").replace("www.", "").split(".")[0].title()
            row["name"] = domain

    # --- Rule 5: Fill missing business_id by domain lookup ---
    domain_to_biz: Dict[str, int] = {}
    for row in deduped:
        biz = row.get("business_id")
        domain = row.get("domain_name", "")
        if biz and domain:
            domain_to_biz[domain] = biz
    for row in deduped:
        if not row.get("business_id"):
            domain = row.get("domain_name", "")
            if domain in domain_to_biz:
                row["business_id"] = domain_to_biz[domain]

    # --- Rule 6: Normalize membership_context ---
    for row in deduped:
        mc = row.get("membership_context")
        if mc == '"not provided"' or mc == "not provided" or not mc:
            row["membership_context"] = None
        else:
            try:
                json.loads(mc)
            except (json.JSONDecodeError, TypeError):
                row["membership_context"] = None

    # Truncate page_content to avoid blowing up LLM calls
    for row in deduped:
        content = row.get("page_content", "") or ""
        if len(content) > MAX_PAGE_CONTENT_CHARS:
            row["page_content"] = content[:MAX_PAGE_CONTENT_CHARS]

    return deduped, skip_log


# ---------------------------------------------------------------------------
# Step 4: QA Checks
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> float:
    try:
        return float(str(v or 0).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def run_qa_checks(offers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run QA checks per PRD §4.2 on the in-memory offer list."""
    results: Dict[str, Any] = {}

    # Check 1: discount_price < regular_price
    price_inversions = [
        o for o in offers
        if o.get("regular_price") is not None and o.get("discount_price") is not None
        and _to_float(o["regular_price"]) > 0
        and _to_float(o["discount_price"]) >= _to_float(o["regular_price"])
    ]
    results["check1_price_inversions"] = {
        "count": len(price_inversions),
        "rows": [{"source_url": o.get("source_url"), "service_name": o.get("service_name"), "regular_price": o.get("regular_price"), "discount_price": o.get("discount_price")} for o in price_inversions[:20]],
    }

    # Check 2: discount_percent in (0, 90]
    bad_pct = [
        o for o in offers
        if o.get("discount_percent") is not None
        and str(o["discount_percent"]).strip()
        and (_to_float(o["discount_percent"]) <= 0 or _to_float(o["discount_percent"]) > 90)
    ]
    results["check2_discount_percent_out_of_range"] = {
        "count": len(bad_pct),
        "rows": [{"source_url": o.get("source_url"), "service_name": o.get("service_name"), "discount_percent": o.get("discount_percent")} for o in bad_pct[:20]],
    }

    # Check 3: each row should have at least one price field
    no_price = [
        o for o in offers
        if o.get("regular_price") is None and o.get("discount_price") is None
        and o.get("discount_percent") is None and o.get("discount_amount") is None
        and o.get("offer_type") not in ("membership", "free_consultation")
    ]
    results["check3_no_price"] = {
        "count": len(no_price),
        "rows": [{"source_url": o.get("source_url"), "service_name": o.get("service_name")} for o in no_price[:20]],
    }

    # Check 4: dedup by (source_url, service_name, offer_raw_text)
    from collections import Counter
    dedup_key_counts = Counter(
        (o.get("source_url", ""), o.get("service_name", ""), (o.get("offer_raw_text") or "")[:100])
        for o in offers
    )
    duplicates = {k: v for k, v in dedup_key_counts.items() if v > 1}
    results["check4_duplicates"] = {
        "count": len(duplicates),
        "rows": [{"source_url": k[0], "service_name": k[1]} for k, v in list(duplicates.items())[:20]],
    }

    # Check 5: business_id fill rate
    with_biz = sum(1 for o in offers if o.get("business_id"))
    total = len(offers)
    results["check5_business_id"] = {
        "with_biz": with_biz,
        "without_biz": total - with_biz,
        "pct": round(with_biz / total * 100, 1) if total else 0,
    }

    # Check 6: offer_type distribution
    type_counts: Dict[str, int] = {}
    for o in offers:
        t = o.get("offer_type")
        if not t:
            t = "null"
        type_counts[t] = type_counts.get(t, 0) + 1
    results["check6_offer_type_distribution"] = type_counts

    null_type_count = type_counts.get("null", 0)
    results["check6_null_type_pct"] = round(null_type_count / total * 100, 1) if total else 0

    return results


def format_qa_report(results: Dict[str, Any]) -> str:
    lines = [
        "# QA Report — Batch Extraction",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Check 1: discount_price < regular_price",
        f"  Count: {results['check1_price_inversions']['count']}",
        "",
        "## Check 2: discount_percent ∈ (0, 90]",
        f"  Count: {results['check2_discount_percent_out_of_range']['count']}",
        "",
        "## Check 3: Each row has ≥1 price field",
        f"  Count (no price, non-membership): {results['check3_no_price']['count']}",
        "",
        "## Check 4: Duplicate offers",
        f"  Count: {results['check4_duplicates']['count']}",
        "",
        "## Check 5: business_id fill rate",
        f"  With: {results['check5_business_id']['with_biz']} / {results['check5_business_id']['with_biz'] + results['check5_business_id']['without_biz']} ({results['check5_business_id']['pct']}%)",
        "",
        "## Check 6: offer_type distribution",
    ]
    for k, v in sorted(results["check6_offer_type_distribution"].items()):
        lines.append(f"  {k}: {v}")
    lines.append(f"  null type pct: {results['check6_null_type_pct']}%")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 3 helper: offer_type classification (PRD §4.1)
# ---------------------------------------------------------------------------

def _infer_offer_type(text: str) -> str:
    """Classify offer_type from raw text per PRD §4.1 keyword matching."""
    if not text:
        return ""

    # Priority order from the PRD
    if re.search(r"(?i)new patient|new client|first time|first visit", text):
        return "new_patient"
    if re.search(r"(?i)membership|per month|monthly|per year|annual|members only", text):
        return "membership"
    if re.search(r"(?i)package|bundle|combo|buy\s+\d+\s+get", text):
        return "package"
    if re.search(r"(?i)gift card|gift certificate", text):
        return "gift_card"
    if re.search(r"(?i)free consultation|complimentary consultation", text):
        return "free_consultation"
    if re.search(r"(?i)%\s*off|\$\d+\s*off|save\s+\$|original price|regular price.*sale price|was.*now|normally", text):
        return "discount"
    if re.search(r"\$\d", text):
        return "general"
    return ""


def _empty_to_null(v: Any) -> Any:
    """Convert empty string to None for nullable fields."""
    if isinstance(v, str) and not v.strip():
        return None
    return v

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch extract promo offers from staging")
    parser.add_argument("--dry-run", action="store_true", help="Extract but don't write to DB; save JSON")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N rows")
    parser.add_argument("--api-url", default=None, help="LLM API URL override")
    parser.add_argument("--model", default=None, help="LLM model override")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()

    # Setup clients
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    db = SupabaseRestClient(supabase_url, supabase_key)

    llm_client = build_client_from_env(api_url=args.api_url, model=args.model)
    if not llm_client:
        print("WARNING: no LLM client configured — extraction will be empty (rule-based only)", file=sys.stderr)

    # Fetch all staging rows
    print("Fetching staging rows...")
    rows = db.fetch_rows(STAGING_TABLE, select="*", order="promo_website_id.asc")
    total_raw = len(rows)
    print(f"  Raw rows: {total_raw}")

    if args.limit:
        rows = rows[:args.limit]

    # Step 1: Clean
    print("Step 1: Data cleaning...")
    clean_rows, skip_log = clean_staging_rows(rows)
    print(f"  Clean rows: {len(clean_rows)}")
    print(f"  Skipped: {len(skip_log)}")
    for s in skip_log:
        print(f"    [{s['skip_reason']}] promo_website_id={s['promo_website_id']} {s.get('subpage_url', '')}")

    # Step 2+3: Extract offers per row
    print("Step 2+3: Extracting offers...")
    all_offers: List[Dict[str, Any]] = []
    extraction_errors: List[Dict[str, Any]] = []

    for idx, row in enumerate(clean_rows):
        try:
            pid = row["promo_website_id"]
            print(f"  [{idx+1}/{len(clean_rows)}] promo_website_id={pid} {row.get('domain_name', '')}...", end="")
            retries = 3
            result = None
            for attempt in range(retries):
                try:
                    result = extract_offers_for_row(row, client=llm_client)
                    break
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "Too Many Requests" in err_str:
                        wait = 60 * (attempt + 1)
                        print(f" 429 (retry {attempt+1}/{retries} after {wait}s)...", end="")
                        import time
                        time.sleep(wait)
                    else:
                        raise
            if result is None:
                print(" ERROR: all retries exhausted")
                extraction_errors.append({"promo_website_id": pid, "error": "retries exhausted"})
                continue
            # Small delay between rows to avoid rate limiting
            import time
            time.sleep(0.5)
            offers = result.get("offers", [])
            print(f" {len(offers)} offers")

            for offer in offers:
                raw_text = (offer.get("offer_raw_text") or "") + " " + (offer.get("service_name") or "")
                offer_type = _infer_offer_type(raw_text)

                normalized = {
                    "source_url": row.get("subpage_url", ""),
                    "source_name": row.get("name", ""),
                    "business_id": row.get("business_id"),
                    "offer_type": offer_type or None,
                    "service_category": _empty_to_null(offer.get("service_category")),
                    "service_name": offer.get("service_name") or "",
                    "offer_raw_text": (offer.get("offer_raw_text") or "")[:2000],
                    "discount_percent": _empty_to_null(offer.get("discount_percent")),
                    "discount_amount": _empty_to_null(offer.get("discount_amount")),
                    "regular_price": _empty_to_null(offer.get("original_price") or offer.get("regular_price")),
                    "discount_price": _empty_to_null(offer.get("discount_price")),
                    "unit_type": _empty_to_null(offer.get("unit_type")),
                    "start_date": None,
                    "end_date": None,
                    "eligibility": None,
                    "is_package": "false",
                    "delivered_unit": None,
                    "min_unit": None,
                    "service_area": None,
                    "offer_content": None,
                    "membership_plan_id": None,
                    "status": "active",
                }

                # is_package detection (PRD §4.6)
                if re.search(r"(?i)(package|bundle|combo|buy\s+\d+.*?get\s+\d+)", raw_text):
                    normalized["is_package"] = "true"

                # eligibility extraction (PRD §4.5)
                elig_parts = []
                if re.search(r"(?i)(new patient|new client|first time|first visit)", raw_text):
                    elig_parts.append("new_patient")
                if re.search(r"(?i)(members? only|vip members|wave club)", raw_text):
                    elig_parts.append("membership_required")
                if re.search(r"(?i)while supplies last", raw_text):
                    elig_parts.append("while_supplies_last")
                if re.search(r"(?i)cannot be combined|non.?combinable", raw_text):
                    elig_parts.append("non_combinable")
                normalized["eligibility"] = ", ".join(elig_parts) if elig_parts else None

                # Filter: skip pure descriptions with no type and no price
                # Exception: keep packages and membership offers even without direct price
                if not normalized["offer_type"] and not any([
                    normalized["regular_price"], normalized["discount_price"],
                    normalized["discount_percent"], normalized["discount_amount"]
                ]):
                    continue

                all_offers.append(normalized)

        except Exception as e:
            pid = row.get("promo_website_id", "?")
            print(f" ERROR: {e}")
            extraction_errors.append({"promo_website_id": pid, "error": str(e)})

    print(f"\nTotal offers extracted: {len(all_offers)}")
    print(f"Extraction errors: {len(extraction_errors)}")

    # Step 4: QA
    print("\nStep 4: QA checks...")
    qa_results = run_qa_checks(all_offers)
    qa_report = format_qa_report(qa_results)
    print(qa_report)

    # Save artifacts
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # Save extraction output
    output_path = OUTPUT_DIR / f"batch_extraction_{timestamp}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "total_raw": total_raw,
            "clean_rows": len(clean_rows),
            "skipped": len(skip_log),
            "skip_log": skip_log,
            "total_offers": len(all_offers),
            "offers": all_offers,
            "extraction_errors": extraction_errors,
        }, f, ensure_ascii=False, indent=2)
    print(f"\nOutput saved: {output_path}")

    # Save QA report
    qa_path = REPORT_DIR / f"batch_extraction_qa_{timestamp}.md"
    with open(qa_path, "w", encoding="utf-8") as f:
        f.write(qa_report)
    print(f"QA report: {qa_path}")

    # Write to DB unless dry-run
    if args.dry_run:
        print("\nDRY RUN — skipping DB write")
        return

    if not all_offers:
        print("No offers to insert, skipping DB write")
        return

    # Insert in batches
    print(f"\nInserting {len(all_offers)} offers into {OFFER_TABLE}...")
    batch_size = 50
    inserted = 0
    for i in range(0, len(all_offers), batch_size):
        batch = all_offers[i:i + batch_size]
        try:
            db.insert_rows(OFFER_TABLE, batch)
            inserted += len(batch)
            if (i + batch_size) % 200 == 0 or (i + batch_size) >= len(all_offers):
                print(f"  Inserted {inserted}/{len(all_offers)}")
        except Exception as e:
            print(f"  Batch insert error at offset {i}: {e}", file=sys.stderr)

    print(f"\nDone. Inserted: {inserted}")

    # Mark staging rows as processed
    print("Marking staging rows as processed...")
    for row in clean_rows:
        try:
            db.update_row(
                STAGING_TABLE,
                {"promo_website_id": f"eq.{row['promo_website_id']}"},
                {"processed_status": True}
            )
        except Exception as e:
            print(f"  Failed to update promo_website_id={row['promo_website_id']}: {e}", file=sys.stderr)

    print("All done.")


if __name__ == "__main__":
    main()
