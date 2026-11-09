import json
import sys
from pathlib import Path

from scripts import recrawl_sources


def catalogue(read_sql: Path) -> Path:
    """只读快照：按 read.sql 的条件返回商家 7 的促销与会员。"""
    tables = {"promotions": [{"promotion_id": 3, "business_id": 7, "source_url": "https://a.test/specials"}],
              "memberships": [{"plan_id": 5, "business_id": 7, "source_url": "https://a.test/membership"}]}
    path = read_sql.parent / "snapshot.json"
    path.write_text(json.dumps({name: {"count": len(rows), "rows": rows} for name, rows in tables.items()}))
    return path


def test_recrawl_decisions_are_written_in_one_step_for_a_business(tmp_path, monkeypatch):
    queries = []

    def read_snapshot(read_sql):
        queries.append(Path(read_sql).read_text())
        return catalogue(Path(read_sql))

    monkeypatch.setattr("utils.pipeline_db.read_snapshot", read_snapshot)
    monkeypatch.setattr(recrawl_sources, "OUTPUT_DIR", tmp_path / "runs")
    monkeypatch.setattr(sys, "argv", ["recrawl-sources", "--business-id", "7"])
    assert recrawl_sources.main() == 0
    assert "from public.clinic_promotions where (business_id = any(array[7]::bigint[]) and is_active)" in queries[0]
    assert "from public.clinic_memberships where (business_id = any(array[7]::bigint[]))" in queries[0]
    (decisions_path,) = (tmp_path / "runs").glob("*/decisions.json")
    assert json.loads(decisions_path.read_text())["decisions"] == [
        {"url": "https://a.test/specials", "business_id": 7, "kind": "promotion", "decision": "add"},
        {"url": "https://a.test/membership", "business_id": 7, "kind": "membership", "decision": "add"}]
