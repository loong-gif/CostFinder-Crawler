"""Verify preprocessing improvement: raw vs filtered page_content."""
import os, sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

# Load env
env_file = project_root / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ[key.strip()] = val.strip().strip('"').strip("'")

import requests
proxies = {"http": "http://192.168.1.189:7890", "https": "http://192.168.1.189:7890"}
base = f"{os.getenv('SUPABASE_URL')}/rest/v1"
key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
h = {"apikey": key, "Authorization": f"Bearer {key}"}

# Grab the barewaxing page (cookie-heavy)
r = requests.get(f"{base}/promo_website_staging", params={
    "select": "promo_website_id,name,page_content",
    "promo_website_id": "eq.2178",
    "limit": "1"
}, headers=h, proxies=proxies, timeout=30)
r.raise_for_status()
rows = r.json()
if not rows:
    print("Row 2178 not found!")
    exit(1)

old_content = rows[0]["page_content"]
print(f"Page: {rows[0]['name']}")
print(f"Current (raw) page_content: {len(old_content)} chars")

# Run through the new preprocessing
from crawler.promo_site_crawler import prepare_page_content, clean_page_text

# We need the original raw content with HTML tags for proper segment extraction.
# The stored page_content is already cleaned markdown. Let's use it as-is with source_type="markdown"
new_result = prepare_page_content(old_content, source_type="markdown")
new_content = new_result["page_content"]
quality_flags = new_result["content_quality_flags"]

print(f"\nPreprocessing result:")
print(f"  Filtered segments: {len(new_result['page_segments_filtered'])} (from {len(new_result['page_segments_raw'])} raw)")
print(f"  New (clean) page_content: {len(new_content)} chars")
print(f"  Compression: {len(new_content)/max(len(old_content),1)*100:.0f}%")
print(f"  Quality flags: {quality_flags}")

# Check noise reduction
old_cookie = old_content.lower().count("cookie") + old_content.lower().count("consent")
new_cookie = new_content.lower().count("cookie") + new_content.lower().count("consent")
old_accept = old_content.lower().count("accept all") + old_content.lower().count("reject all")
new_accept = new_content.lower().count("accept all") + new_content.lower().count("reject all")
old_skip = old_content.lower().count("skip to content")
new_skip = new_content.lower().count("skip to content")

print(f"\nNoise reduction:")
print(f"  'cookie/consent': {old_cookie} → {new_cookie}")
print(f"  'accept/reject':  {old_accept} → {new_accept}")
print(f"  'skip to content': {old_skip} → {new_skip}")

# Show first 500 chars of old vs new
print(f"\n=== OLD (raw) first 500 chars ===")
print(old_content[:500])
print(f"\n=== NEW (clean) first 500 chars ===")
print(new_content[:500])

# Also check the amoderm page
print(f"\n\n{'='*60}")
print("Testing amoderm page (id=2167)")
r2 = requests.get(f"{base}/promo_website_staging", params={
    "select": "promo_website_id,name,page_content",
    "promo_website_id": "eq.2167",
    "limit": "1"
}, headers=h, proxies=proxies, timeout=30)
r2.raise_for_status()
rows2 = r2.json()
if rows2:
    old2 = rows2[0]["page_content"]
    new2_result = prepare_page_content(old2, source_type="markdown")
    new2 = new2_result["page_content"]
    print(f"{rows2[0]['name']}: {len(old2)} → {len(new2)} chars ({len(new2)/max(len(old2),1)*100:.0f}%)")
    print(f"Flags: {new2_result['content_quality_flags']}")
    # Show key pricing lines
    for line in new2.split("\n")[:15]:
        if "$" in line or "price" in line.lower() or "special" in line.lower():
            print(f"  {line.strip()[:100]}")
