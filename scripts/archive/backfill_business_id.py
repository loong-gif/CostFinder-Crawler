#!/usr/bin/env python3
"""Backfill null business_id in promo_offer_master via curl + REST API batch PATCH."""
from __future__ import annotations

import json
import os
import subprocess
import re
import sys
import time
from collections import Counter
from urllib.parse import urlparse

PROXY = "http://192.168.1.189:7890"
SUPABASE_URL = "https://kdlpkjzcnbkjcvwsvlwn.supabase.co"
APIKEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

def curl(method, path, data=None):
    cmd = [
        "curl", "-x", PROXY, "-s", "--connect-timeout", "10", "--max-time", "30",
        "-H", f"apikey: {APIKEY}",
        "-H", f"Authorization: Bearer {APIKEY}",
        "-H", "Content-Type: application/json",
        "-X", method,
        f"{SUPABASE_URL}/rest/v1/{path}",
    ]
    if data:
        cmd.extend(["-d", json.dumps(data)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed (rc={result.returncode}): {result.stderr[:200]}")
    if result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()
    return None

def extract_domain(raw):
    """Normalize any URL/domain to a clean domain without www."""
    if not raw:
        return ""
    s = raw.strip().lower()
    # Full URL
    if s.startswith("http://") or s.startswith("https://"):
        netloc = urlparse(s).netloc
    else:
        netloc = s
    netloc = netloc.removeprefix("www.").split("/")[0].split("?")[0].strip()
    return netloc

def domain_match(a, b):
    """Match domain a against domain b with subdomain support."""
    if a == b:
        return True
    # Handle subdomain mismatch: lp.h-md.com vs h-md.com
    parts_a = a.split(".")
    parts_b = b.split(".")
    # Take last 2 parts (e.g., h-md.com)
    if len(parts_a) >= 2 and len(parts_b) >= 2:
        suffix_a = ".".join(parts_a[-2:])
        suffix_b = ".".join(parts_b[-2:])
        if suffix_a == suffix_b:
            return True
    return False

print("=== P0-1: Backfill business_id ===")

# Step 1: Load all master_business_info domains
print("\n[1/4] Loading master_business_info website_clean...")
biz_map = {}  # domain -> business_id
biz_suffix = {}  # 2-part suffix -> business_id (fallback)
rows = curl("GET", "master_business_info?select=business_id,website_clean&website_clean=not.is.null&limit=500")
for row in rows:
    domain = extract_domain(row.get("website_clean", ""))
    if domain and domain not in biz_map:
        biz_map[domain] = row["business_id"]
        parts = domain.split(".")
        if len(parts) >= 2:
            suffix = ".".join(parts[-2:])
            if suffix not in biz_suffix:
                biz_suffix[suffix] = row["business_id"]
print(f"  {len(biz_map)} unique domains, {len(biz_suffix)} unique suffixes")

# Step 2: Load ALL null business_id rows (paginate)
print("\n[2/4] Loading all null business_id rows...")
null_rows = []
offset = 0
while True:
    rows = curl("GET", f"promo_offer_master?select=id,source_url&business_id=is.null&limit=500&offset={offset}")
    if not rows:
        break
    null_rows.extend(rows)
    offset += 500
    print(f"  loaded {len(null_rows)}...")
print(f"  total {len(null_rows)} null rows")

# Step 3: Match with exact + suffix fallback
print("\n[3/4] Matching domains...")
matched = []  # [(id, business_id), ...]
unmatched = Counter()
suffix_matched = 0

for row in null_rows:
    domain = extract_domain(row.get("source_url", ""))
    if not domain:
        continue
    if domain in biz_map:
        matched.append((row["id"], biz_map[domain]))
    else:
        # Try suffix match
        parts = domain.split(".")
        if len(parts) >= 2:
            suffix = ".".join(parts[-2:])
            if suffix in biz_suffix:
                matched.append((row["id"], biz_suffix[suffix]))
                suffix_matched += 1
            else:
                unmatched[domain] += 1
        else:
            unmatched[domain] += 1

print(f"  Exact matches: {len(matched) - suffix_matched}")
print(f"  Suffix matches: {suffix_matched}")
print(f"  Total matched: {len(matched)} / {len(null_rows)}")
print(f"  Unmatched domains: {len(unmatched)}")

# Show unmatched sample
print("\n  Unmatched domains (top 30):")
for d, c in unmatched.most_common(30):
    print(f"    {d:50s} {c}")

# Step 4: PATCH in batches
print(f"\n[4/4] Patching {len(matched)} rows via REST API...")
patched = 0
errors = 0
for row_id, biz_id in matched:
    try:
        curl("PATCH", f"promo_offer_master?id=eq.{row_id}", data={"business_id": biz_id})
        patched += 1
    except Exception as e:
        errors += 1
        if errors <= 5:
            print(f"  ERROR id={row_id}: {e}")
    if patched % 500 == 0:
        print(f"  progress: {patched}/{len(matched)}")

print(f"\nDone: {patched} patched, {errors} errors")

# Verify
remaining = curl("GET", "promo_offer_master?select=count&business_id=is.null")
cnt = remaining[0]["count"] if isinstance(remaining, list) else "?"
print(f"Remaining NULL business_id: {cnt}")
