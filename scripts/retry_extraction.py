#!/usr/bin/env python3
"""Retry failed extraction rows with delay to avoid 429 rate limits."""
from __future__ import annotations

import json, os, sys, time
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))

from utils.supabase_rest import SupabaseRestClient
from utils.offer_extraction_llm import build_client_from_env, extract_offers_for_row

load_dotenv()

DB = SupabaseRestClient(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
LLM = build_client_from_env()
OFFER_TABLE = "promo_offer_master"
STAGING_TABLE = "promo_website_staging"

# Existing extraction output
EXISTING = PROJECT_ROOT / "output" / "results" / "batch_extraction_20260708_160224.json"
exists = json.loads(EXISTING.read_text("utf-8"))
existing_ids = set()
for o in exists.get("offers", []):
    if o.get("source_url"): existing_ids.add(o["source_url"])

# Fetch all staging rows, find unprocessed ones
staging = DB.fetch_rows(STAGING_TABLE, select="*", order="promo_website_id.asc")
to_extract = [r for r in staging if not r.get("processed_status") and r.get("promo_website_id") not in {s.get("promo_website_id") for s in exists.get("skip_log", [])}]
# Filter to only errored rows (those without existing offers)
to_retry = [r for r in to_extract if r.get("subpage_url") not in existing_ids]
print(f"Staging total: {len(staging)}, already processed: {len(staging)-len(to_extract)}, errored: {len(to_retry)}")

all_new = []
for idx, row in enumerate(to_retry):
    pid = row["promo_website_id"]
    print(f"  [{idx+1}/{len(to_retry)}] pid={pid} {row.get('domain_name','')}...", end="", flush=True)
    try:
        result = extract_offers_for_row(row, client=LLM)
        offers = result.get("offers", [])
        print(f" {len(offers)} offers") if offers else print(" 0 offers", end="")
    except Exception as e:
        print(f" ERR: {str(e)[:60]}")
        time.sleep(30)  # rate limit backoff
        continue

    for offer in offers:
        raw_text = (offer.get("offer_raw_text") or "") + " " + (offer.get("service_name") or "")
        def _inf(t):
            if not t: return None
            if __import__("re").search(r"(?i)new patient|new client|first time|first visit", t): return "new_patient"
            if __import__("re").search(r"(?i)membership|per month|monthly|per year|annual|members only", t): return "membership"
            if __import__("re").search(r"(?i)package|bundle|combo|buy\s+\d+\s+get", t): return "package"
            if __import__("re").search(r"(?i)gift card|gift certificate", t): return "gift_card"
            if __import__("re").search(r"(?i)free consultation|complimentary consultation", t): return "free_consultation"
            if __import__("re").search(r"(?i)%\s*off|\$\d+\s*off|save\s+\$|original price|regular price.*sale price|was.*now|normally", t): return "discount"
            if __import__("re").search(r"\$\d", t): return "general"
            return None
        ot = _inf(raw_text)
        n = {
            "channel": "Website",
            "source_url": row.get("subpage_url",""),
            "source_name": row.get("name",""),
            "template_type": ot or "",
            "service_category": offer.get("service_category") or None,
            "service_name": offer.get("service_name") or "",
            "offer_raw_text": (offer.get("offer_raw_text") or "")[:2000],
            "regular_price": (lambda v: None if v is None or (isinstance(v,str) and not v.strip()) else (float(str(v).replace(",","")) if str(v).strip() else None))(offer.get("original_price") or offer.get("regular_price")),
            "discount_price": (lambda v: None if v is None or (isinstance(v,str) and not v.strip()) else (float(str(v).replace(",","")) if str(v).strip() else None))(offer.get("discount_price")),
            "discount_percent": (lambda v: None if v is None or (isinstance(v,str) and not v.strip()) else (float(str(v)) if str(v).strip() else None))(offer.get("discount_percent")),
            "discount_amount": offer.get("discount_amount") or None,
            "unit_type": offer.get("unit_type") or None,
            "is_package": "TRUE" if __import__("re").search(r"(?i)(package|bundle|combo|buy\s+\d+.*?get\s+\d+)", raw_text) else "FALSE",
            "is_membership_required": "TRUE" if ot == "membership" else "FALSE",
            "eligibility": None,
            "service_area": None,
            "start_date": None, "end_date": None,
            "business_id": row.get("business_id") or None,
            "delivered_unit": None, "min_unit": None,
            "membership_plan_id": None, "offer_content": None,
            "status": "active",
        }
        # Skip pure descriptions
        if not ot and not any([n["regular_price"], n["discount_price"], n["discount_percent"], n["discount_amount"]]): continue
        all_new.append({k:v for k,v in n.items() if v is not None})

    # Pony tail: 2s delay between rows to avoid rate limit
    time.sleep(2)

print(f"\nNew offers to insert: {len(all_new)}")
if not all_new: print("Nothing to insert."); sys.exit(0)

# Insert
for i in range(0, len(all_new), 50):
    batch = all_new[i:i+50]
    try:
        DB.insert_rows(OFFER_TABLE, batch)
        print(f"  Inserted {min(i+50, len(all_new))}/{len(all_new)}")
    except Exception as e:
        for r in batch:
            try: DB.insert_rows(OFFER_TABLE, [r]); print(f"  +1")
            except: pass

# Mark processed
for row in to_retry:
    try:
        DB.update_row(STAGING_TABLE, {"promo_website_id": f"eq.{row['promo_website_id']}"}, {"processed_status": True})
    except: pass
print("Done.")
