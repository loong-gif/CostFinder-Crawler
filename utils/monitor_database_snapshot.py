"""监控主机上的只读数据库快照：经 Supabase REST 读取（监控主机没有 Supabase CLI，read-snapshot 只在本机运行）。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from utils.monitor_price_writer_plan import SERVICE_COLUMNS
from utils.pipeline_db import write_text
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key

PROJECT_ID = "kdlpkjzcnbkjcvwsvlwn"
PAGE_SIZE = 500
PRIMARY_KEYS = {"clinic_promotions": "promotion_id", "promo_offer_items": "offer_item_id",
                "master_business_info": "business_id", "clinic_services": "service_id",
                "promo_offer_terms": "offer_id", "clinic_memberships": "plan_id"}


def rest_client() -> SupabaseRestClient:
    base = os.getenv("SUPABASE_URL", "")
    if urlsplit(base).hostname != PROJECT_ID + ".supabase.co":
        raise RuntimeError("database_project_mismatch")
    return SupabaseRestClient(base, get_supabase_secret_key())


def all_rows(client: Any, table: str, columns: str = "*") -> list[dict]:
    values, offset = [], 0
    order = f"{PRIMARY_KEYS.get(table, 'id')}.asc"
    while True:
        rows = client.fetch_rows(table, columns, limit=PAGE_SIZE, offset=offset, order=order)
        values.extend(rows)
        if len(rows) < PAGE_SIZE:
            return values
        offset += len(rows)


def rest_snapshot(directory: Path, tables: Mapping[str, tuple[str, str, str]]) -> Path:
    """监控主机上代替 pipeline_db.fresh_snapshot：同样的表定义经 REST 整表读取，按 read_snapshot 的格式
    写 directory/snapshot.json 并返回其路径。where 是 SQL 条件，REST 无法执行，Python 也不解析 SQL，
    所以只接受整表（where 为 true），其他条件直接终止。"""
    client = rest_client()
    payload = {}
    for name, (source, columns, where) in tables.items():
        if where != "true":
            raise ValueError(f"rest_read_needs_whole_table: {name} where {where}")
        rows = all_rows(client, source, columns.replace(" ", ""))
        payload[name] = {"count": len(rows), "rows": rows}
    return write_text(Path(directory) / "snapshot.json", json.dumps(payload, ensure_ascii=False))


def database_snapshot() -> dict:
    client = rest_client()
    snapshot = {name: all_rows(client, table) for name, table in {
        "promotions": "clinic_promotions", "offers": "promo_offer_master", "items": "promo_offer_items",
        "businesses": "master_business_info"}.items()}
    snapshot["services"] = all_rows(client, "clinic_services", SERVICE_COLUMNS)
    snapshot["terms"] = all_rows(client, "promo_offer_terms")
    # Read only source ownership, never membership fees or benefits.
    snapshot["memberships"] = all_rows(client, "clinic_memberships", "plan_id,business_id,source_url")
    names = client.rpc_read("fn_clinic_service_names")
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        raise RuntimeError("service_names_unavailable")
    snapshot["service_names"] = names
    return snapshot
