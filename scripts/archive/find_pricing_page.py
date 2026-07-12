"""Find a staging row with good pricing content."""
import os, requests
from pathlib import Path

env_file = Path(__file__).resolve().parents[1] / ".env"
for line in env_file.read_text().splitlines():
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        k,_,v = line.partition('=')
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

proxies = {"http": "http://192.168.1.189:7890", "https": "http://192.168.1.189:7890"}
base = f"{os.getenv('SUPABASE_URL')}/rest/v1"
h = {"apikey": os.getenv("SUPABASE_SERVICE_ROLE_KEY"), "Authorization": f"Bearer {os.getenv('SUPABASE_SERVICE_ROLE_KEY')}"}

r = requests.get(f"{base}/promo_website_staging", params={
    "select": "promo_website_id,subpage_url,name,page_content",
    "limit": "50", "order": "promo_website_id.desc"
}, headers=h, proxies=proxies, timeout=30)
rows = r.json()

pages = []
for row in rows:
    content = row.get("page_content") or ""
    dc = content.count("$")
    if dc >= 5:
        pages.append((row["promo_website_id"], row.get("name",""), row.get("subpage_url",""), dc, len(content), content))

pages.sort(key=lambda x: x[3], reverse=True)
for pid, name, url, dc, cl, _ in pages[:15]:
    print(f"id={pid:5d} | ${dc:3d} | {cl:6d}c | {name[:55]}")
    print(f"         {url[:90]}")

# Save the best one's ID for next test
if pages:
    best = pages[0]
    print(f"\nBEST: id={best[0]} with ${best[3]} price signs, {best[4]} chars")
    # Write first 3000 chars of content to a temp file
    with open("/tmp/test_page_content.txt", "w") as f:
        f.write(best[5][:3000])
    print("Saved to /tmp/test_page_content.txt")
