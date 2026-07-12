"""Diagnose promo_offer_master quality - v5: use correct columns."""
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

def api_get(endpoint, params=None):
    r = requests.get(f"{base}/{endpoint}", params=params, headers=headers, proxies=proxies, timeout=30)
    r.raise_for_status()
    return r.json()

def count_rows(table, **filters):
    params = {"select": "count", **filters, "limit": "1"}
    return api_get(table, params)[0]["count"]

def fetch_rows(table, columns="*", limit=500, **filters):
    params = {"select": columns, "limit": str(limit), **filters}
    return api_get(table, params)

def sf(val):
    return str(val or "")

active = count_rows("promo_offer_master", status="neq.ended")

# 1. Source distribution (≈domain)
print("=== Offers by source_name (top 25) ===")
rows = fetch_rows("promo_offer_master", "source_name", status="neq.ended", limit=5000)
sources = Counter(row.get("source_name", "(null)") for row in rows)
for src, cnt in sources.most_common(25):
    print(f"  {cnt:4d}  {sf(src)[:60]}")

# 2. Channel distribution
print(f"\n=== Channel distribution ===")
rows = fetch_rows("promo_offer_master", "channel", status="neq.ended", limit=5000)
channels = Counter(row.get("channel", "(null)") for row in rows)
for ch, cnt in channels.most_common(10):
    print(f"  {cnt:4d}  {ch}")

# 3. membership_plan_id
has_plan = count_rows("promo_offer_master", membership_plan_id="not.is.null", status="neq.ended")
is_member = count_rows("promo_offer_master", is_membership_required="eq.true", status="neq.ended")
print(f"\nHas membership_plan_id: {has_plan}")
print(f"is_membership_required=true: {is_member}")

# 4. discount_price=0
print(f"\n=== discount_price=0 ===")
rows = fetch_rows("promo_offer_master",
    "id,service_name,regular_price,discount_price,discount_percent,source_name",
    discount_price="eq.0", status="neq.ended", limit=20)
for row in rows:
    print(f"  id={row['id']:5d} | {sf(row['service_name'])[:40]:40s} | reg={row.get('regular_price')} pct={row.get('discount_percent')} | {sf(row.get('source_name'))[:40]}")

# 5. Gift cards + non-treatment
print(f"\n=== Non-treatment offers ===")
for kw in ["gift card", "giftcard", "consult", "product", "skincare", "retail", "shop"]:
    try:
        rows = fetch_rows("promo_offer_master",
            "id,service_name,regular_price,discount_price,service_category,source_name",
            service_name=f"ilike.*{kw}*", status="neq.ended", limit=10)
        for row in rows:
            print(f"  [{kw}] id={row['id']:5d} | {sf(row['service_name'])[:40]:40s} | cat={sf(row.get('service_category'))[:20]} | reg={row.get('regular_price')} disc={row.get('discount_price')} | {sf(row.get('source_name'))[:30]}")
    except Exception as e:
        pass

# 6. Business_id=NULL
orphans = count_rows("promo_offer_master", business_id="is.null", status="neq.ended")
print(f"\nActive offers with business_id=NULL: {orphans}")

# 7. Cross-page merge candidates: discount-only offers per source_name
print(f"\n=== Discount-only (no regular_price) per source (top 15 sources) ===")
rows = fetch_rows("promo_offer_master",
    "source_name,regular_price,discount_price",
    regular_price="is.null", discount_price="not.is.null", status="neq.ended", limit=5000)
disc_only_by_source = Counter(row.get("source_name", "(null)") for row in rows)
for src, cnt in disc_only_by_source.most_common(15):
    total = sources.get(src, 1)
    print(f"  {cnt:3d}/{total:3d} disc-only  {sf(src)[:60]}")

# 8. promo_membership_plans
print(f"\n=== promo_membership_plans ===")
mp_total = count_rows("promo_membership_plans")
print(f"Total: {mp_total}")
rows = fetch_rows("promo_membership_plans", "plan_name,monthly_fee,business_id", limit=20)
for row in rows:
    print(f"  {sf(row.get('plan_name'))[:40]:40s} | monthly={row.get('monthly_fee')} | biz={row.get('business_id')}")
