"""Test onlyMainContent via self-hosted Firecrawl tunnel."""
import os, json, requests, time

api_url = "http://localhost:3003"
api_key = os.getenv("FIRECRAWL_API_KEY", "") 

# Read from .env
from pathlib import Path
env_file = Path(__file__).resolve().parents[1] / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == "FIRECRAWL_API_KEY":
                api_key = v.strip().strip('"').strip("'")

url = "https://www.amoderm.com/cosmedical-treatments-special-offers-ultherapy"
headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

# ON
t0 = time.time()
r = requests.post(f"{api_url}/v1/scrape", json={
    "url": url, "formats": ["markdown"], "onlyMainContent": True
}, headers=headers, timeout=60)
d_on = r.json()
content_on = d_on.get("data", {}).get("markdown", "")
t1 = time.time()

# OFF
r2 = requests.post(f"{api_url}/v1/scrape", json={
    "url": url, "formats": ["markdown"], "onlyMainContent": False
}, headers=headers, timeout=60)
d_off = r2.json()
content_off = d_off.get("data", {}).get("markdown", "")
t2 = time.time()

print(f"ON  ({t1-t0:.1f}s): {len(content_on)} chars")
print(f"  cookie: {'cookie' in content_on.lower()}")
print(f"  consent: {'consent' in content_on.lower()}")
print(f"  accept all: {'accept all' in content_on.lower()}")
print(f"  price signs: {content_on.count('$')}")

print(f"\nOFF ({t2-t1:.1f}s): {len(content_off)} chars")
print(f"  cookie: {'cookie' in content_off.lower()}")
print(f"  consent: {'consent' in content_off.lower()}")
print(f"  accept all: {'accept all' in content_off.lower()}")
print(f"  price signs: {content_off.count('$')}")

print(f"\nCompression: {len(content_on)}/{len(content_off)} = {len(content_on)/max(len(content_off),1)*100:.0f}%")
print(f"Price preserved: {content_on.count('$')}/{content_off.count('$')}")

print("\n=== ON first 400 ===")
print(content_on[:400])

print("\n=== OFF first 400 ===")
print(content_off[:400])
