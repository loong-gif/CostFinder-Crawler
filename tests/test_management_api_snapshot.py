from __future__ import annotations

import json
import os

import pytest
import requests

from utils.pipeline_db import SERVICE_NAME_ENUM_SNAPSHOT, load_snapshot, snapshot_query, write_snapshot

# 经 Supabase Management API 读取线上生产库（只读事务）；本机先导出 SUPABASE_READONLY_TOKEN 再运行
requires_readonly_token = pytest.mark.skipif(
    not os.getenv("SUPABASE_READONLY_TOKEN"), reason="export SUPABASE_READONLY_TOKEN to read the production database")


@pytest.mark.parametrize("rows", [[], [{"snapshot": {}}, {"snapshot": {}}], [{"snapshot": {}, "extra": 1}]])
def test_a_result_that_is_not_one_snapshot_value_is_rejected(tmp_path, rows):
    with pytest.raises(RuntimeError, match="unexpected snapshot result shape"):
        write_snapshot(tmp_path, rows)
    assert not (tmp_path / "snapshot.json").exists()


def test_the_snapshot_value_is_saved_as_snapshot_json(tmp_path):
    path = write_snapshot(tmp_path, [{"snapshot": {"a": {"count": 1, "rows": [{"name": "Ápice"}]}}}])
    assert path.read_text(encoding="utf-8") == '{"a": {"count": 1, "rows": [{"name": "Ápice"}]}}'


@requires_readonly_token
def test_api_snapshot_runs_the_read_sql_with_its_where_clauses(tmp_path):
    from utils.monitor_database_snapshot import api_snapshot
    tables = {"active": ("clinic_promotions", "promotion_id, is_active", "is_active"),
              "inactive": ("clinic_promotions", "promotion_id, is_active", "not is_active"),
              "enum": SERVICE_NAME_ENUM_SNAPSHOT}
    path = api_snapshot(tmp_path, tables)
    assert path == tmp_path / "snapshot.json"
    # 与本机 read_snapshot 执行的是同一条查询
    assert (tmp_path / "read.sql").read_text() == snapshot_query(tables)
    snapshot = load_snapshot(path, tables, typed=False)
    assert snapshot["active"] and all(row["is_active"] is True for row in snapshot["active"])
    assert snapshot["inactive"] and all(row["is_active"] is False for row in snapshot["inactive"])
    assert set(json.loads(path.read_text())) == set(tables)


@requires_readonly_token
def test_the_read_only_endpoint_rejects_writes():
    from utils.monitor_database_snapshot import run_read_only
    with pytest.raises(requests.HTTPError, match="400"):
        run_read_only("insert into public.clinic_services (service_id) values (-1);")


@requires_readonly_token
def test_database_snapshot_reads_every_monitor_table_and_the_service_names():
    from utils.monitor_database_snapshot import database_snapshot
    snapshot = database_snapshot()
    assert set(snapshot) == {"promotions", "offers", "items", "businesses", "services", "terms", "memberships",
                             "service_names"}
    assert all(snapshot[name] for name in snapshot)
    assert set(snapshot["services"][0]) == {"service_id", "business_id", "service_name", "service_area", "source_url"}
    assert set(snapshot["memberships"][0]) == {"plan_id", "business_id", "source_url"}
    assert "promotion_id" in snapshot["promotions"][0] and "offer_item_id" in snapshot["items"][0]
    # 服务名称按枚举顺序排列，与 fn_clinic_service_names 一致
    assert all(isinstance(name, str) for name in snapshot["service_names"])
    assert len(set(snapshot["service_names"])) == len(snapshot["service_names"])
    # 时间戳保持 JSON 原值，与按 --database-snapshot 重放的文件一致
    assert isinstance(snapshot["promotions"][0]["updated_at"], str)
