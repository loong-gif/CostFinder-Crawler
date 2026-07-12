"""Check template_type, unit_type, and complex pricing patterns."""
import os
from collections import Counter
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
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
headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}

def fetch_rows(table, columns="*", limit=500, **filters):
    params = {"select": columns, "limit": str(limit), **filters}
    r = requests.get(f"{base}/{table}", params=params, headers=headers, proxies=proxies, timeout=30)
    r.raise_for_status()
    return r.json()

def sf(val):
    return str(val or "")

# template_type distribution
print("=== template_type distribution (active) ===")
rows = fetch_rows("promo_offer_master", "template_type", status="neq.ended", limit=5000)
templates = Counter(sf(row.get("template_type")) for row in rows)
for t, c in templates.most_common(20):
    print(f"  {c:4d}  '{t[:60]}'")

# unit_type distribution
print(f"\n=== unit_type distribution (active) ===")
rows = fetch_rows("promo_offer_master", "unit_type", status="neq.ended", limit=5000)
units = Counter(sf(row.get("unit_type")) for row in rows)
for u, c in units.most_common(20):
    print(f"  {c:4d}  '{u[:60]}'")

# Samples with template_type set
print(f"\n=== Samples with template_type set ===")
rows = fetch_rows("promo_offer_master",
    "id,service_name,template_type,offer_raw_text,regular_price,discount_price,unit_type",
    template_type="not.is.null", status="neq.ended", limit=20)
for row in rows:
    raw = (row.get("offer_raw_text") or "")[:120]
    print(f"  id={row['id']:5d} | tpl={sf(row['template_type'])[:25]:25s} | unit={sf(row['unit_type'])[:10]} | reg={row.get('regular_price')} disc={row.get('discount_price')} | {raw}")

# Offers with "buy" in offer_raw_text (promotions)
print(f"\n=== Offers with 'buy' or 'get' in raw_text ===")
rows = fetch_rows("promo_offer_master",
    "id,service_name,offer_raw_text,regular_price,discount_price,template_type",
    offer_raw_text="ilike.*buy*", status="neq.ended", limit=20)
for row in rows:
    raw = (row.get("offer_raw_text") or "")[:150]
    print(f"  id={row['id']:5d} | tpl={sf(row['template_type'])[:15]} | reg={row.get('regular_price')} disc={row.get('discount_price')} | {raw}")

# Percentage-based offers
print(f"\n=== Offers with discount_percent set ===")
rows = fetch_rows("promo_offer_master",
    "id,service_name,offer_raw_text,regular_price,discount_price,discount_percent",
    discount_percent="not.is.null", status="neq.ended", limit=20)
for row in rows:
    raw = (row.get("offer_raw_text") or "")[:120]
    print(f"  id={row['id']:5d} | reg={row.get('regular_price')} disc={row.get('discount_price')} pct={row.get('discount_percent')}% | {raw}")
