"""监控主机上的每日 quality：主机没有 Supabase CLI，快照经 Supabase REST 整表读取。"""
from __future__ import annotations

import json

import pytest

from scripts import run_data_quality_audit as audit
from scripts.deploy_monitor_price_sync import data_quality_launcher
from utils.data_quality_writer import SNAPSHOT_TABLES


class PagedRest:
    """Supabase REST 的分页读取：按 limit/offset 返回内存中的表。"""
    def __init__(self, tables):
        self.tables, self.calls = tables, []

    def fetch_rows(self, table, select, *, limit, offset, order):
        self.calls.append((table, select, offset, order))
        return self.tables[table][offset:offset + limit]


def test_quality_snapshot_tables_are_whole_tables_so_rest_can_read_them():
    assert all(where == "true" for _, _, where in SNAPSHOT_TABLES.values())


def test_quality_with_rest_reads_every_table_over_rest_without_the_supabase_cli(tmp_path, monkeypatch, capsys):
    promotions = [{"promotion_id": i, "business_id": 1, "is_active": False, "source_url": f"https://a.test/p{i}"}
                  for i in range(1, 702)]
    rest = PagedRest({"clinic_promotions": promotions, "clinic_memberships": [], "promo_offer_master": [],
                      "promo_offer_items": [], "clinic_services": [], "promo_offer_terms": []})
    monkeypatch.setattr("utils.monitor_database_snapshot.rest_client", lambda: rest)
    monkeypatch.setattr(audit, "fresh_snapshot", lambda *a: pytest.fail("Supabase CLI used"))
    monkeypatch.setattr("sys.argv", ["quality", "--state-dir", str(tmp_path), "--rest"])
    audit.main()
    assert {call[0] for call in rest.calls} == {table for table, _, _ in SNAPSHOT_TABLES.values()}
    assert ("clinic_promotions", "*", 500, "promotion_id.asc") in rest.calls
    assert json.loads(capsys.readouterr().out)["issues"] == 0
    # REST 读到的整表照样存成 snapshot.json，之后可用 --snapshot 重放同一次审计
    saved = next((tmp_path / "data_quality" / "runs").glob("*/snapshot.json"))
    assert json.loads(saved.read_text())["promotions"] == {"count": 701, "rows": promotions}


def test_rest_and_a_saved_snapshot_cannot_be_combined(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["quality", "--state-dir", str(tmp_path), "--rest", "--snapshot", "s.json"])
    with pytest.raises(SystemExit):
        audit.main()


def test_daily_quality_on_the_monitor_host_reads_over_rest():
    launcher = data_quality_launcher().decode()
    assert "promo_pipeline.py\" quality --state-dir \"$BASE/state\" --rest --recheck" in launcher
