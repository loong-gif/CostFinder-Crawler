from __future__ import annotations

import json
import os

import pytest

from scripts import run_data_quality_audit as audit
from scripts.deploy_monitor_price_sync import data_quality_launcher
from utils.data_quality_writer import SNAPSHOT_TABLES

# 监控主机上的每日 quality：主机没有 Supabase CLI，经 Management API 执行同一条只读快照查询
requires_readonly_token = pytest.mark.skipif(
    not os.getenv("SUPABASE_READONLY_TOKEN"), reason="export SUPABASE_READONLY_TOKEN to read the production database")


@requires_readonly_token
def test_quality_over_the_management_api_reads_without_the_supabase_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(audit, "fresh_snapshot", lambda *a: pytest.fail("Supabase CLI used"))
    monkeypatch.setattr("sys.argv", ["quality", "--state-dir", str(tmp_path), "--management-api"])
    audit.main()
    assert "issues" in json.loads(capsys.readouterr().out)
    # 读到的快照照样存成 snapshot.json，之后可用 --snapshot 重放同一次审计
    saved = json.loads(next((tmp_path / "data_quality" / "runs").glob("*/snapshot.json")).read_text())
    assert set(saved) == set(SNAPSHOT_TABLES)
    assert all(part["count"] == len(part["rows"]) and part["rows"] for part in saved.values())


def test_management_api_and_a_saved_snapshot_cannot_be_combined(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["quality", "--state-dir", str(tmp_path), "--management-api",
                                     "--snapshot", "s.json"])
    with pytest.raises(SystemExit):
        audit.main()


def test_daily_quality_on_the_monitor_host_reads_over_the_management_api():
    launcher = data_quality_launcher().decode()
    assert "promo_pipeline.py\" quality --state-dir \"$BASE/state\" --management-api --recheck" in launcher
