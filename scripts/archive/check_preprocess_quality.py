"""Compare raw vs preprocessed page content quality."""
import os, json
from pathlib import Path

env_file = Path(__file__).resolve().parents[1] / ".env"
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

# Get the cookie-heavy page + 2 others
for pid in ["2178", "2167", "2143"]:
    r = requests.get(f"{base}/promo_website_staging", params={
        "select": "promo_website_id,name,subpage_url,page_content,page_content_llm,content_quality_flags",
        "promo_website_id": f"eq.{pid}",
        "limit": "1"
    }, headers=h, proxies=proxies, timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        continue
    row = rows[0]
    raw = row.get("page_content") or ""
    llm = row.get("page_content_llm") or ""
    flags = row.get("content_quality_flags", "[]")
    
    if not raw:
        continue
    
    # Count noise
    raw_cookie = raw.lower().count("cookie") + raw.lower().count("consent")
    llm_cookie = llm.lower().count("cookie") + llm.lower().count("consent")
    raw_accept = raw.lower().count("accept all") + raw.lower().count("reject all")
    llm_accept = llm.lower().count("accept all") + llm.lower().count("reject all")
    raw_nav = raw.lower().count("skip to content") + raw.lower().count("directions")
    llm_nav = llm.lower().count("skip to content") + llm.lower().count("directions")
    
    print(f"\n{'='*50}")
    print(f"id={row['promo_website_id']} | {row.get('name','')[:50]}")
    print(f"URL: {row.get('subpage_url','')[:90]}")
    print(f"Raw: {len(raw)} chars → LLM: {len(llm)} chars ({len(llm)/max(len(raw),1)*100:.0f}%)")
    print(f"Flags: {flags}")
    print(f"  Cookie/consent:  {raw_cookie} → {llm_cookie}")
    print(f"  Accept/Reject:   {raw_accept} → {llm_accept}")
    print(f"  Nav/skip/direct: {raw_nav} → {llm_nav}")
    
    # Show beginning of LLM content
    if llm:
        print(f"\n  LLM content first 400 chars:")
        print(f"  {llm[:400]}")
        print(f"  ...")
        print(f"  LLM content last 200 chars:")
        print(f"  ...{llm[-200:]}")
