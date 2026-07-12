"""Compare local model extraction: raw vs cleaned page_content."""
import os, json, time, requests, re, sys
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
base = f"{os.getenv('SUPABASE_URL')}/rest/v1"
key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
h = {"apikey": key, "Authorization": f"Bearer {key}"}

from crawler.promo_site_crawler import prepare_page_content

# Pick a pricing-rich page
r = requests.get(f"{base}/promo_website_staging", params={
    "select": "promo_website_id,name,subpage_url,page_content",
    "promo_website_id": "eq.2166",  # amoderm - Ultherapy specials
    "limit": "1"
}, headers=h, proxies=proxies, timeout=30)
r.raise_for_status()
row = r.json()[0]
raw_content = row["page_content"]
source_url = row.get("subpage_url", "")

# Preprocess
processed = prepare_page_content(raw_content, source_type="markdown")
clean_content = processed["page_content"]
flags = processed["content_quality_flags"]

print(f"Source: {row['name']}")
print(f"URL: {source_url}")
print(f"Raw: {len(raw_content)} chars → Clean: {len(clean_content)} chars ({len(clean_content)/max(len(raw_content),1)*100:.0f}%)")
print(f"Flags: {flags}")

CATS = "Neurotoxins, Fillers & Other Injectables, Body Contouring, Laser & Light, Skin Rejuvenation, Facial, Hair Removal, IV Therapy, Weight Loss, Sexual Wellness, Tattoo Removal, Microneedling, PRP/PRF, Other"

PROMPT = f"""Extract every priced service offer from this medspa webpage content.

CRITICAL RULES:
1. EVERY offer MUST have at least one of: regular_price or discount_price set to a NUMBER.
   If the text says "$850", set regular_price: 850 (or discount_price: 850).
   NEVER leave both prices null if a dollar amount appears in the text.
2. service_category MUST be exactly one of: {CATS}
3. template_type MUST be one of: FIXED_PRICE, DISCOUNT, BUNDLE
4. unit_type: "unit" for per-unit neurotoxins, "syringe" for fillers, "treatment" for sessions, "area" for body areas, "vial" for vials, "" if unclear.
5. EXCLUDE: memberships, VIP plans, free consultations, gift cards, rewards programs, retail products.
6. For "was X now Y": regular_price=X, discount_price=Y, template_type=DISCOUNT
7. For flat pricing: regular_price=X, template_type=FIXED_PRICE
8. For bundles (buy N get M free etc): template_type=BUNDLE, compute effective per-unit price

Respond with ONLY this JSON:
{{"offers": [{{"service_name": "...", "service_category": "...", "regular_price": null, "discount_price": null, "discount_percent": null, "unit_type": "...", "template_type": "...", "offer_raw_text": "..."}}]}}

Page content:
{clean_content[:4000]}"""

print(f"\nPrompt: {len(PROMPT)} chars")
print(f"Content preview:\n{clean_content[:500]}\n")

print("="*50)
print("TEST 1: schematron-8b on CLEANED content")
print("="*50)

t0 = time.time()
resp = requests.post("http://192.168.1.189:1234/v1/chat/completions", json={
    "model": "schematron-8b",
    "messages": [{"role": "user", "content": PROMPT}],
    "temperature": 0.0,
    "max_tokens": 2000,
}, timeout=300)
elapsed = time.time() - t0

body = resp.json()
content = body["choices"][0]["message"]["content"]
usage = body.get("usage", {})
print(f"Time: {elapsed:.1f}s | Tokens: {usage.get('prompt_tokens')}→{usage.get('completion_tokens')}")

# Parse
clean = content.strip()
if clean.startswith("```"):
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.DOTALL).strip()

try:
    parsed = json.loads(clean)
    offers = parsed.get("offers", [])
    print(f"\nExtracted {len(offers)} offers:")
    for i, o in enumerate(offers):
        has_price = "💰" if (o.get("regular_price") or o.get("discount_price") or o.get("discount_percent")) else "❌"
        sn = str(o.get("service_name", ""))[:40]
        print(f"  [{i}] {has_price} {sn:40s} | reg={str(o.get('regular_price')):>6} disc={str(o.get('discount_price')):>6} | unit={str(o.get('unit_type',''))[:10]:10s} tpl={o.get('template_type','')}")
except Exception as e:
    print(f"JSON error: {e}")
    print(f"Raw response:\n{content[:600]}")

# Compare with raw content (same prompt but unprocessed)
print(f"\n{'='*50}")
print("TEST 2: schematron-8b on RAW content (for comparison)")
print("="*50)

raw_prompt = PROMPT.replace(clean_content[:4000], raw_content[:4000])
t0 = time.time()
resp2 = requests.post("http://192.168.1.189:1234/v1/chat/completions", json={
    "model": "schematron-8b",
    "messages": [{"role": "user", "content": raw_prompt}],
    "temperature": 0.0,
    "max_tokens": 2000,
}, timeout=300)
elapsed2 = time.time() - t0

body2 = resp2.json()
content2 = body2["choices"][0]["message"]["content"]
usage2 = body2.get("usage", {})
print(f"Time: {elapsed2:.1f}s | Tokens: {usage2.get('prompt_tokens')}→{usage2.get('completion_tokens')}")

clean2 = content2.strip()
if clean2.startswith("```"):
    clean2 = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean2, flags=re.DOTALL).strip()

try:
    parsed2 = json.loads(clean2)
    offers2 = parsed2.get("offers", [])
    print(f"\nExtracted {len(offers2)} offers:")
    for i, o in enumerate(offers2):
        has_price = "💰" if (o.get("regular_price") or o.get("discount_price") or o.get("discount_percent")) else "❌"
        sn = str(o.get("service_name", ""))[:40]
        print(f"  [{i}] {has_price} {sn:40s} | reg={str(o.get('regular_price')):>6} disc={str(o.get('discount_price')):>6} | unit={str(o.get('unit_type',''))[:10]:10s} tpl={o.get('template_type','')}")
except Exception as e:
    print(f"JSON error: {e}")
    print(f"Raw response:\n{content2[:600]}")

# Summary
print(f"\n{'='*50}")
print("COMPARISON SUMMARY")
print(f"{'='*50}")
print(f"Cleaned: {len(offers)} offers in {elapsed:.1f}s ({len(clean_content)} chars input)")
print(f"Raw:     {len(offers2)} offers in {elapsed2:.1f}s ({len(raw_content)} chars input)")
