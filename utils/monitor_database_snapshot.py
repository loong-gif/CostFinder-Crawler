"""监控主机上的只读数据库快照：经 Supabase REST 读取促销、优惠、商家、服务与会员来源（不读会员费与权益）。"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from utils.monitor_price_writer_plan import SERVICE_COLUMNS
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key

PROJECT_ID = "kdlpkjzcnbkjcvwsvlwn"


def database_snapshot() -> dict:
    base = os.getenv("SUPABASE_URL", "")
    if urlsplit(base).hostname != PROJECT_ID + ".supabase.co":
        raise RuntimeError("database_project_mismatch")
    client = SupabaseRestClient(base, get_supabase_secret_key())
    def all_rows(table, columns="*"):
        values, offset = [], 0
        pk = {"clinic_promotions":"promotion_id", "promo_offer_items":"offer_item_id",
              "master_business_info":"business_id", "clinic_services":"service_id",
              "promo_offer_terms":"offer_id", "clinic_memberships":"plan_id"}.get(table, "id")
        while True:
            rows = client.fetch_rows(table, columns, limit=500, offset=offset, order=f"{pk}.asc")
            values.extend(rows)
            if len(rows) < 500:
                return values
            offset += len(rows)
    snapshot = {name:all_rows(table) for name,table in {
        "promotions":"clinic_promotions", "offers":"promo_offer_master", "items":"promo_offer_items",
        "businesses":"master_business_info"}.items()}
    snapshot["services"] = all_rows("clinic_services", SERVICE_COLUMNS)
    snapshot["terms"] = all_rows("promo_offer_terms")
    # Read only source ownership, never membership fees or benefits.
    snapshot["memberships"] = all_rows("clinic_memberships", "plan_id,business_id,source_url")
    names = client.rpc_read("fn_clinic_service_names")
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        raise RuntimeError("service_names_unavailable")
    snapshot["service_names"] = names
    return snapshot
