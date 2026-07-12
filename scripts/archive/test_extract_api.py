"""Test Firecrawl extract API for structured offer extraction."""
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

CATS = "Neurotoxins, Fillers & Other Injectables, Body Contouring, Laser & Light, Skin Rejuvenation, Facial, Hair Removal, IV Therapy, Weight Loss, Sexual Wellness, Tattoo Removal, Microneedling, PRP/PRF, Other"

# Test 1: extract with schema on a pricing page
print("=== Test 1: extract with schema ===")
url = "https://www.amoderm.com/cosmedical-treatments-special-offers-ultherapy"

schema = {
    "type": "object",
    "properties": {
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service_name": {"type": "string"},
                    "service_category": {"type": "string", "enum": CATS.split(", ")},
                    "regular_price": {"type": "number"},
                    "discount_price": {"type": "number"},
                    "discount_percent": {"type": "number"},
                    "unit_type": {"type": "string"},
                    "template_type": {"type": "string", "enum": ["FIXED_PRICE", "DISCOUNT", "BUNDLE"]},
                }
            }
        }
    }
}

prompt = (
    "Extract every priced aesthetic service offer from this page. "
    "EXCLUDE: memberships, VIP plans, free consultations, gift cards, rewards programs. "
    "For 'was X now Y' pricing: regular_price=X, discount_price=Y, template_type=DISCOUNT. "
    "For flat pricing: regular_price=X, template_type=FIXED_PRICE. "
    "For bundles: template_type=BUNDLE. "
    "If no price at all, do NOT include. "
    "unit_type: 'unit' for per-unit neurotoxins, 'syringe' for fillers, 'treatment' for sessions, 'vial' for vials."
)

t0 = time.time()
r = requests.post(f"{api_url}/v1/extract", json={
    "urls": [url],
    "prompt": prompt,
    "schema": schema,
}, headers=headers, timeout=120)
t1 = time.time()

print(f"Status: {r.status_code} | Time: {t1-t0:.1f}s")
if r.status_code == 200:
    data = r.json()
    print(f"Success: {data.get('success')}")
    result = data.get("data", {})
    offers = result.get("offers", [])
    print(f"Offers: {len(offers)}")
    for i, o in enumerate(offers):
        print(f"  [{i}] {o.get('service_name','')[:40]:40s} | reg={o.get('regular_price')} disc={o.get('discount_price')} | unit={o.get('unit_type','')} tpl={o.get('template_type','')}")
    print(f"\nFull response keys: {list(data.keys())}")
    if "data" in data and isinstance(data["data"], dict):
        print(f"Data keys: {list(data['data'].keys())}")
elif r.status_code == 404:
    print(f"404 - extract endpoint not available on self-hosted")
    print(f"Response: {r.text[:300]}")
else:
    print(f"Error: {r.text[:500]}")
