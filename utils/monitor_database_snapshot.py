from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

import requests
from dotenv import load_dotenv

from utils.monitor_price_writer_plan import SERVICE_COLUMNS
from utils.offer_schema import service_name_enum
from utils.pipeline_db import SERVICE_NAME_ENUM_SNAPSHOT, load_snapshot, snapshot_query, write_snapshot, write_text
from utils.supabase_rest import PRODUCTION_PROJECT_REF

# 监控主机没有 Supabase CLI：经 Management API 的只读端点执行与本机 read_snapshot 相同的 read.sql
QUERY_URL = f"https://api.supabase.com/v1/projects/{PRODUCTION_PROJECT_REF}/database/query/read-only"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
STATE_DIR = Path(__file__).resolve().parents[1] / "state" / "database_snapshot"
MONITOR_TABLES = {
    "promotions": ("clinic_promotions", "*", "true"),
    "offers": ("promo_offer_master", "*", "true"),
    "items": ("promo_offer_items", "*", "true"),
    "businesses": ("master_business_info", "*", "true"),
    "services": ("clinic_services", SERVICE_COLUMNS, "true"),
    "terms": ("promo_offer_terms", "*", "true"),
    # 只读来源归属，不读会员费用与权益
    "memberships": ("clinic_memberships", "plan_id, business_id, source_url", "true"),
    "service_enum": SERVICE_NAME_ENUM_SNAPSHOT,
}


def run_read_only(query: str) -> list[dict[str, Any]]:
    """在只读事务中执行查询并返回结果行；写入语句由数据库拒绝（SQLSTATE 25006）。"""
    # 主机上的启动脚本不导出令牌，从部署目录的 .env 读取
    load_dotenv(ENV_FILE)
    session = requests.Session()
    session.trust_env = False
    response = session.post(QUERY_URL, json={"query": query}, timeout=600,
                            headers={"Authorization": f"Bearer {os.environ['SUPABASE_READONLY_TOKEN']}"})
    response.raise_for_status()
    return response.json()


def api_snapshot(directory: Path, tables: Mapping[str, tuple[str, str, str]]) -> Path:
    """监控主机上代替 pipeline_db.fresh_snapshot：写入 directory/read.sql 并执行，返回 snapshot.json 的路径。"""
    read_sql = write_text(Path(directory) / "read.sql", snapshot_query(tables))
    return write_snapshot(directory, run_read_only(read_sql.read_text()))


def database_snapshot() -> dict:
    # 调用方自行保存需要留存的快照；read.sql 与 snapshot.json 只在本次读取期间存在，并发任务各用各的目录
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=STATE_DIR) as directory:
        snapshot = load_snapshot(api_snapshot(Path(directory), MONITOR_TABLES), MONITOR_TABLES, typed=False)
    snapshot["service_names"] = service_name_enum(snapshot.pop("service_enum"))
    return snapshot
