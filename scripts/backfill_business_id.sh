#!/usr/bin/env bash
set -euo pipefail
# Backfill null business_id in promo_offer_master via curl through proxy.
# Requires SUPABASE_SERVICE_ROLE_KEY and SUPABASE_URL in environment.

PROXY="http://192.168.1.189:7890"

curl_api() {
    local method="$1" path="$2" data="${3:-}"
    curl -x "$PROXY" -s --connect-timeout 10 --max-time 30 \
        -H "apikey: ${SUPABASE_SERVICE_ROLE_KEY}" \
        -H "Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}" \
        -H "Content-Type: application/json" \
        -X "$method" \
        "${SUPABASE_URL}/rest/v1/${path}" \
        ${data:+-d "$data"}
}

echo "Fetching null business_id rows..."
ROWS=$(curl_api GET "promo_offer_master?select=offer_id,domain_name&business_id=is.null&limit=10")
echo "$ROWS" | python3 -c "
import json,sys
rows=json.load(sys.stdin)
print(f'Found {len(rows)} rows with null business_id')
for r in rows:
    print(f\"  {r['offer_id']}: {r.get('domain_name','?')}\")
"
