from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from utils import pipeline_db

# 写库命令：plan 步骤不带 --snapshot 时自行生成 read.sql 并读取快照
WRITE_COMMANDS = ["delete_offer_shells", "enforce_promotion_rules", "split_offers", "merge_promotions",
                  "link_services", "extract_offers", "rename_items", "upload_sheet_memberships", "extract_pages"]


def _fake_cli(payload: dict, seen: list[Path]):
    """代替 Supabase CLI：记录被执行的 read.sql，按真实 read_snapshot 的约定写出同目录 snapshot.json。"""
    def read(read_sql: Path) -> Path:
        seen.append(read_sql)
        return pipeline_db.write_text(read_sql.parent / "snapshot.json", json.dumps(payload))
    return read


def test_fresh_snapshot_writes_read_sql_and_returns_the_snapshot(tmp_path, monkeypatch):
    seen: list[Path] = []
    monkeypatch.setattr(pipeline_db, "read_snapshot", _fake_cli({"t": {"count": 0, "rows": []}}, seen))
    path = pipeline_db.fresh_snapshot(tmp_path / "run", {"t": ("clinic_promotions", "promotion_id", "false")})
    assert seen == [tmp_path / "run" / "read.sql"]
    assert "from public.clinic_promotions where false" in seen[0].read_text()
    assert pipeline_db.load_snapshot(path, ["t"]) == {"t": []}


def test_plan_without_snapshot_reads_the_database_automatically(tmp_path, monkeypatch, capsys):
    from scripts import delete_offer_shells
    from utils.offer_shells import SNAPSHOT_TABLES
    seen: list[Path] = []
    monkeypatch.setattr(pipeline_db, "read_snapshot",
                        _fake_cli({name: {"count": 0, "rows": []} for name in SNAPSHOT_TABLES}, seen))
    monkeypatch.setattr(delete_offer_shells, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["delete-offer-shells", "plan"])
    assert delete_offer_shells.main() == 0
    plan = json.loads((seen[0].parent / "plan.json").read_text())
    assert seen[0].parent.parent == tmp_path and plan["delete"] == [] and plan["blocked"] == []


def test_merge_verify_rereads_the_database_after_apply(tmp_path, monkeypatch, capsys):
    from scripts import merge_promotions
    from utils.promotion_merge import SNAPSHOT_TABLES
    seen: list[Path] = []
    monkeypatch.setattr(pipeline_db, "read_snapshot",
                        _fake_cli({name: {"count": 0, "rows": []} for name in SNAPSHOT_TABLES}, seen))
    plan = pipeline_db.write_text(tmp_path / "plan.json", json.dumps({"groups": []}))
    monkeypatch.setattr(sys, "argv", ["merge-promotions", "verify", "--plan", str(plan)])
    assert merge_promotions.main() == 0
    assert seen == [tmp_path / "after" / "read.sql"]
    assert "verified: database matches plan" in capsys.readouterr().out


@pytest.mark.parametrize("module", WRITE_COMMANDS)
def test_write_commands_have_no_manual_read_sql_step(module, monkeypatch):
    script = importlib.import_module(f"scripts.{module}")
    monkeypatch.setattr(sys, "argv", [module, "read-sql"])
    with pytest.raises(SystemExit):
        script.main()
    assert "read-sql" not in (script.__doc__ or "")
