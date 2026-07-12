#!/usr/bin/env python3
"""Re-scrape all staging URLs with onlyMainContent=True and update page_content."""
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

proxies = {"http": "http://192.168.1.189:7890", "https": "http://192.168.1.189:7890"}
supabase_base = f"{os.getenv('SUPABASE_URL')}/rest/v1"
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase_h = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}

firecrawl_url = "http://localhost:3003"
firecrawl_key = os.getenv("FIRECRAWL_API_KEY", "")
firecrawl_h = {"Authorization": f"Bearer {firecrawl_key}", "Content-Type": "application/json"}

# Fetch all staging URLs with their IDs and current content
print("Fetching staging URLs...")
r = requests.get(f"{supabase_base}/promo_website_staging", params={
    "select": "promo_website_id,subpage_url,page_content",
    "order": "promo_website_id.asc",
    "limit": "5000",
}, headers=supabase_h, proxies=proxies, timeout=60)
r.raise_for_status()
rows = r.json()
print(f"Found {len(rows)} staging rows")

# Batch processing
BATCH_SIZE = 5
SLEEP_BETWEEN = 0.5  # seconds between scrapes
total_updated = 0
total_skipped = 0
total_failed = 0

for i in range(0, len(rows), BATCH_SIZE):
    batch = rows[i:i + BATCH_SIZE]
    
    for row in batch:
        pid = row["promo_website_id"]
        url = row["subpage_url"]
        old_content = row.get("page_content") or ""
        old_len = len(old_content)
        
        if not url:
            total_skipped += 1
            continue
        
        try:
            # Scrape with cleaning options
            r2 = requests.post(f"{firecrawl_url}/v1/scrape", json={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "blockAds": True,
            }, headers=firecrawl_h, timeout=120)
            r2.raise_for_status()
            data = r2.json()
            new_content = data.get("data", {}).get("markdown", "")
            
            if not new_content:
                print(f"  [{pid}] {url[:60]} - EMPTY response, skipping")
                total_skipped += 1
                continue
            
            new_len = len(new_content)
            
            # Only update if content actually changed meaningfully
            if new_content == old_content:
                total_skipped += 1
                continue
            
            # Update Supabase
            r3 = requests.patch(
                f"{supabase_base}/promo_website_staging",
                params={"promo_website_id": f"eq.{pid}"},
                headers={**supabase_h, "Prefer": "return=minimal"},
                json={
                    "page_content": new_content,
                    "last_updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                },
                proxies=proxies,
                timeout=30,
            )
            r3.raise_for_status()
            
            pct = new_len / max(old_len, 1) * 100
            print(f"  [{pid}] {url[:60]} — {old_len}→{new_len} chars ({pct:.0f}%)")
            total_updated += 1
            
        except requests.HTTPError as e:
            status = e.response.status_code if hasattr(e, 'response') else '?'
            print(f"  [{pid}] {url[:60]} — ERROR {status}: {str(e)[:100]}")
            total_failed += 1
        except Exception as e:
            print(f"  [{pid}] {url[:60]} — ERROR: {str(e)[:100]}")
            total_failed += 1
        
        time.sleep(SLEEP_BETWEEN)
    
    # Progress
    if (i + BATCH_SIZE) % 50 == 0:
        done = min(i + BATCH_SIZE, len(rows))
        print(f"\n--- Progress: {done}/{len(rows)} | Updated: {total_updated} | Skipped: {total_skipped} | Failed: {total_failed} ---\n")

print(f"\n{'='*50}")
print(f"DONE: {total_updated} updated, {total_skipped} skipped, {total_failed} failed")
