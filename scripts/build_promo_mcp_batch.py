#!/usr/bin/env python3
"""Emit a base64 JSON batch for the transactional Supabase MCP importer."""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
from textwrap import dedent
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.promo_csv_import import build_import_plan, normalize_source_url, source_domain
from utils.schema_contract import TABLE_CLINIC_PROMOTIONS, TABLE_MASTER_BUSINESS
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key


def fetch_all(client: SupabaseRestClient, table: str, select: str) -> list[dict]:
    result: list[dict] = []
    offset = 0
    while True:
        page = client.fetch_rows(table, select, limit=1000, offset=offset)
        result.extend(page)
        if len(page) < 1000:
            return result
        offset += len(page)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--sql", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    client = SupabaseRestClient(os.environ["SUPABASE_URL"], get_supabase_secret_key())
    businesses = fetch_all(client, TABLE_MASTER_BUSINESS, "business_id,name,website")
    promotions = fetch_all(client, TABLE_CLINIC_PROMOTIONS, "promotion_id,business_id,source_url,is_active")
    by_url = {normalize_source_url(row.get("source_url")): row for row in promotions}
    by_domain: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for business in businesses:
        domain = source_domain(business.get("website"))
        if domain:
            by_domain.setdefault(domain, []).append(business)
        name = " ".join(str(business.get("name") or "").split()).casefold()
        if name:
            by_name.setdefault(name, []).append(business)
    with args.csv_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    plan = build_import_plan(rows, promotions_by_url=by_url, businesses_by_domain=by_domain, businesses_by_name=by_name)
    payload: list[dict] = []
    for entry in plan.accepted[args.offset : args.offset + args.limit]:
        source = rows[entry.source_row - 2]
        safe_items = []
        for item in entry.items:
            safe_item = dict(item)
            quantity = safe_item.get("quantity")
            if isinstance(quantity, float) and not quantity.is_integer():
                safe_item.pop("quantity", None)
            safe_items.append(safe_item)
        payload.append({
            "import_key": str(entry.source_row),
            "business_id": entry.master["business_id"],
            "source_url": normalize_source_url(source["source_url"]),
            "promotion_title": source["source_name"].strip() or "Imported promotion",
            "campaign_start_date": source["start_date"].strip() or None,
            "campaign_end_date": source["end_date"].strip() or None,
            "membership_name": source["membership_name"].strip() or None,
            "membership_price": source["membership_price"].strip() or None,
            "billing_period": "quarterly" if "3 month" in source["billing_period"].casefold() else source["billing_period"].strip() if source["billing_period"].strip() in {"monthly", "quarterly", "annual", "one_time"} else "monthly",
            "master": entry.master,
            "items": safe_items,
        })
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    if not args.sql:
        print(encoded)
        return 0
    print(dedent(f"""
    BEGIN; SELECT pg_advisory_xact_lock(hashtextextended('promo_csv_import_v1',0));
    CREATE TEMP TABLE r ON COMMIT DROP AS SELECT * FROM jsonb_to_recordset(convert_from(decode('{encoded}','base64'),'utf8')::jsonb) AS x(import_key text,business_id bigint,source_url text,promotion_title text,campaign_start_date date,campaign_end_date date,membership_name text,membership_price numeric,billing_period billing_period_enum,master jsonb,items jsonb);
    INSERT INTO clinic_promotions(business_id,source_url,promotion_title,campaign_start_date,campaign_end_date,is_active) SELECT DISTINCT ON(business_id,source_url) business_id,source_url,promotion_title,campaign_start_date,campaign_end_date,COALESCE((master->>'is_active')::boolean,true) FROM r ORDER BY business_id,source_url,import_key ON CONFLICT(business_id,source_url) DO NOTHING;
    INSERT INTO clinic_memberships(business_id,membership_name,membership_price,billing_period,source_url) SELECT DISTINCT ON(r.business_id,r.membership_name) r.business_id,r.membership_name,r.membership_price,r.billing_period,r.source_url FROM r WHERE r.membership_name IS NOT NULL AND r.membership_price IS NOT NULL AND NOT EXISTS(SELECT 1 FROM clinic_memberships c WHERE c.business_id=r.business_id AND upper(c.membership_name)=upper(r.membership_name)) ORDER BY r.business_id,r.membership_name,r.import_key;
    CREATE TEMP TABLE resolved ON COMMIT DROP AS SELECT r.*,p.promotion_id,mp.plan_id AS membership_plan_id,encode(extensions.digest(convert_to((CASE WHEN mp.plan_id IS NOT NULL THEN 'm:'||mp.plan_id::text WHEN p.promotion_id IS NOT NULL THEN 'p:'||p.promotion_id::text ELSE 'b:'||r.business_id::text END)||'|'||btrim(lower(regexp_replace(regexp_replace(coalesce(r.master->>'offer_raw_text',''),'[^a-zA-Z0-9]+',' ','g'),'\\s+',' ','g'))),'UTF8'),'sha1'),'hex') AS computed_fingerprint FROM r JOIN clinic_promotions p ON p.business_id=r.business_id AND p.source_url=r.source_url LEFT JOIN LATERAL(SELECT plan_id FROM clinic_memberships c WHERE c.business_id=r.business_id AND upper(c.membership_name)=upper(r.membership_name) LIMIT 1)mp ON true;
    INSERT INTO promo_offer_master(business_id,promotion_id,membership_plan_id,regular_price,discount_price,discount_percent,discount_amount,is_membership_required,offer_fingerprint,offer_raw_text,is_active,is_new_customer_required,price_model) SELECT r.business_id,r.promotion_id,r.membership_plan_id,NULLIF(r.master->>'regular_price','')::numeric,NULLIF(r.master->>'discount_price','')::numeric,NULLIF(r.master->>'discount_percent','')::numeric,NULLIF(r.master->>'discount_amount','')::numeric,CASE WHEN COALESCE((r.master->>'is_membership_required')::boolean,false) AND r.membership_plan_id IS NULL THEN NULL ELSE COALESCE((r.master->>'is_membership_required')::boolean,false) END,r.computed_fingerprint,r.master->>'offer_raw_text',false,COALESCE((r.master->>'is_new_customer_required')::boolean,false),r.master->>'price_model' FROM resolved r WHERE NOT EXISTS(SELECT 1 FROM promo_offer_master e WHERE e.offer_fingerprint=r.computed_fingerprint AND e.is_active IS NOT DISTINCT FROM COALESCE((r.master->>'is_active')::boolean,true)) AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r.items) AS staged(item) LEFT JOIN LATERAL (SELECT 1 AS matched FROM clinic_services s WHERE s.business_id=r.business_id AND upper(s.service_name)=upper(COALESCE(staged.item->>'item_name',staged.item->>'service_name')) LIMIT 1) AS match ON true WHERE match.matched IS NULL) ON CONFLICT(offer_fingerprint) WHERE is_active AND offer_fingerprint IS NOT NULL DO NOTHING;
    INSERT INTO promo_offer_items(offer_id,item_name,service_id,delivered_units,unit_price) SELECT m.id,COALESCE(NULLIF(i->>'item_name',''),NULLIF(i->>'service_name',''),s.service_name),s.service_id,NULLIF(i->>'quantity','')::smallint,NULLIF(i->>'unit_price','')::numeric FROM resolved r JOIN LATERAL(SELECT id FROM promo_offer_master m WHERE m.offer_fingerprint=r.computed_fingerprint AND m.is_active IS NOT DISTINCT FROM COALESCE((r.master->>'is_active')::boolean,true) ORDER BY id DESC LIMIT 1)m ON true CROSS JOIN LATERAL jsonb_array_elements(r.items)i JOIN LATERAL(SELECT service_id,service_name FROM clinic_services s WHERE s.business_id=r.business_id AND upper(s.service_name)=upper(COALESCE(i->>'item_name',i->>'service_name')) ORDER BY service_id LIMIT 1)s ON true WHERE COALESCE(NULLIF(i->>'item_name',''),NULLIF(i->>'service_name',''),s.service_name) IS NOT NULL AND NOT EXISTS(SELECT 1 FROM promo_offer_items oi WHERE oi.offer_id=m.id AND oi.service_id IS NOT DISTINCT FROM s.service_id AND oi.delivered_units IS NOT DISTINCT FROM NULLIF(i->>'quantity','')::smallint);
    UPDATE promo_offer_master m SET is_active=true,updated_at=now() FROM resolved r WHERE m.offer_fingerprint=r.computed_fingerprint AND NOT m.is_active AND COALESCE((r.master->>'is_active')::boolean,true) AND NOT EXISTS (SELECT 1 FROM promo_offer_items i LEFT JOIN clinic_services s ON s.service_id=i.service_id WHERE i.offer_id=m.id AND (i.service_id IS NULL OR s.service_id IS NULL OR s.business_id IS DISTINCT FROM m.business_id OR i.item_name IS DISTINCT FROM s.service_name OR (m.price_model='per_unit' AND (m.discount_price IS NULL OR i.unit_price IS NULL OR abs(i.unit_price-m.discount_price)>0.011))));
    SELECT (SELECT count(*) FROM r) staged; COMMIT;
    """))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
