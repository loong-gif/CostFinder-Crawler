#!/usr/bin/env python3
"""Run custom preprocessing on all staging page_content."""
import os, sys, time, requests
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

env_file = project_root / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ[key.strip()] = val.strip().strip('"').strip("'")

from crawler.promo_site_crawler import prepare_page_content

proxies = {"http": "http://192.168.1.189:7890", "https": "http://192.168.1.189:7890"}
supabase_base = f"{os.getenv('SUPABASE_URL')}/rest/v1"
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase_h = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}

# Fetch all staging rows
print("Fetching staging rows...")
r = requests.get(f"{supabase_base}/promo_website_staging", params={
    "select": "promo_website_id,subpage_url,page_content,name",
    "order": "promo_website_id.asc",
    "limit": "5000",
}, headers=supabase_h, proxies=proxies, timeout=60)
r.raise_for_status()
rows = r.json()
print(f"Found {len(rows)} rows")

total = 0
cookie_cleaned = 0
skipped = 0

for row in rows:
    pid = row["promo_website_id"]
    old_content = row.get("page_content") or ""
    
    # Skip empty content
    if not old_content.strip():
        skipped += 1
        continue
    
    # Run preprocessing
    processed = prepare_page_content(old_content, source_type="markdown")
    new_content = processed["page_content"]  # This is now the clean version
    flags = processed["content_quality_flags"]
    
    # Only update if content changed
    if new_content == old_content:
        skipped += 1
        continue
    
    # Count cookie removal
    old_cookie = old_content.lower().count("cookie") + old_content.lower().count("consent")
    new_cookie = new_content.lower().count("cookie") + new_content.lower().count("consent")
    if old_cookie > new_cookie:
        cookie_cleaned += 1
    
    # Update Supabase
    try:
        requests.patch(
            f"{supabase_base}/promo_website_staging",
            params={"promo_website_id": f"eq.{pid}"},
            headers={**supabase_h, "Prefer": "return=minimal"},
            json={
                "page_content": new_content,
                "last_updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            },
            proxies=proxies,
            timeout=15,
        ).raise_for_status()
        
        pct = len(new_content) / max(len(old_content), 1) * 100
        print(f"  [{pid}] {row.get('name','')[:30]:30s} {len(old_content)}→{len(new_content)} ({pct:.0f}%) {'🍪' if old_cookie > new_cookie else ''}")
        total += 1
        
    except Exception as e:
        print(f"  [{pid}] UPDATE ERROR: {e}")

print(f"\nDone: {total} updated, {cookie_cleaned} cookie-cleaned, {skipped} skipped")
