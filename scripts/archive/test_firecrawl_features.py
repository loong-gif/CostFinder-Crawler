"""Test Firecrawl v2 features: JSON extraction, blockAds, actions."""
import os, json, requests, time
from pathlib import Path

env_file = Path(__file__).resolve().parents[1] / ".env"
api_key = ""
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == "FIRECRAWL_API_KEY":
                api_key = v.strip().strip('"').strip("'")

api_url = "http://localhost:3003"
headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
url = "https://www.amoderm.com/cosmedical-treatments-special-offers-ultherapy"

CATS = "Neurotoxins, Fillers & Other Injectables, Body Contouring, Laser & Light, Skin Rejuvenation, Facial, Hair Removal, IV Therapy, Weight Loss, Sexual Wellness, Tattoo Removal, Microneedling, PRP/PRF, Other"

# === Test 1: v2 scrape with JSON extraction format ===
print("=" * 60)
print("TEST 1: v2 scrape with JSON extraction built-in")
print("=" * 60)

extract_schema = {
    "type": "object",
    "properties": {
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service_name": {"type": "string"},
                    "service_category": {"type": "string"},
                    "regular_price": {"type": "number"},
                    "discount_price": {"type": "number"},
                    "discount_percent": {"type": "number"},
                    "unit_type": {"type": "string"},
                    "template_type": {"type": "string"},
                }
            }
        }
    }
}

t0 = time.time()
r = requests.post(f"{api_url}/v2/scrape", json={
    "url": url,
    "formats": ["markdown", {
        "type": "json",
        "prompt": (
            "Extract every priced aesthetic service offer. "
            "EXCLUDE: memberships, VIP plans, free consultations, gift cards. "
            "For 'was X now Y': regular_price=X, discount_price=Y, template_type=DISCOUNT. "
            "For flat pricing: regular_price=X, template_type=FIXED_PRICE. "
            "For bundles: template_type=BUNDLE. If no price, do NOT include. "
            f"service_category must be one of: {CATS}. "
            "unit_type: 'unit' for neurotoxins, 'syringe' for fillers, 'treatment' for sessions, 'vial' for vials."
        ),
        "schema": extract_schema,
    }],
    "onlyMainContent": True,
}, headers=headers, timeout=120)
t1 = time.time()

print(f"Status: {r.status_code} | Time: {t1-t0:.1f}s")
data = r.json()
if data.get("success"):
    result = data.get("data", {})
    # Check json extraction
    json_data = result.get("json", {}) or result.get("extract", {})
    if isinstance(json_data, str):
        json_data = json.loads(json_data)
    offers = json_data.get("offers", [])
    print(f"  Extracted {len(offers)} offers:")
    for i, o in enumerate(offers):
        print(f"  [{i}] {o.get('service_name','')[:40]:40s} | reg={o.get('regular_price')} disc={o.get('discount_price')} | unit={o.get('unit_type','')} tpl={o.get('template_type','')}")
    
    # Show all top-level keys
    print(f"\n  Response keys: {list(result.keys())}")
else:
    print(f"Error: {data.get('error', data.get('message', str(data)[:300]))}")

# === Test 2: blockAds ===
print(f"\n{'='*60}")
print("TEST 2: blockAds=True")
print("=" * 60)
t0 = time.time()
r = requests.post(f"{api_url}/v1/scrape", json={
    "url": url,
    "formats": ["markdown"],
    "onlyMainContent": True,
    "blockAds": True,
}, headers=headers, timeout=60)
t1 = time.time()
print(f"Status: {r.status_code} | Time: {t1-t0:.1f}s")
if r.status_code == 200:
    d = r.json()
    content = d.get("data", {}).get("markdown", "")
    print(f"  Content: {len(content)} chars | blockAds supported: True")
elif r.status_code == 400:
    print(f"  blockAds not supported on self-hosted: {r.text[:200]}")
else:
    print(f"  Error: {r.text[:200]}")

# === Test 3: map endpoint ===
print(f"\n{'='*60}")
print("TEST 3: map endpoint (discover URLs)")
print("=" * 60)
t0 = time.time()
r = requests.post(f"{api_url}/v1/map", json={
    "url": "https://www.amoderm.com",
    "limit": 10,
}, headers=headers, timeout=60)
t1 = time.time()
print(f"Status: {r.status_code} | Time: {t1-t0:.1f}s")
if r.status_code == 200:
    d = r.json()
    links = d.get("links", d.get("data", {}).get("links", []))
    print(f"  Discovered {len(links)} URLs:")
    for link in links[:15]:
        print(f"    {link}")
else:
    print(f"  Error/not supported: {r.text[:200]}")
