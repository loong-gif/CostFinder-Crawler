from pathlib import Path

import json
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from utils.scheduled_discovery import city_check_due, pending_status, run_batch, settle_sql


def test_pending_sql_waits_for_confirmation_and_detects_rejection(tmp_path: Path):
    pending = tmp_path / "pending_sql"
    pending.mkdir()
    name = "20261111T043000_discovery_pages_0.sql"
    (pending / name).write_text("DO $$ BEGIN NULL; END $$;")
    assert pending_status(pending, name) == "waiting"
    (pending / "applied").mkdir()
    (pending / name).rename(pending / "applied" / name)
    assert pending_status(pending, name) == "applied"
    (pending / "rejected").mkdir()
    (pending / "applied" / name).rename(pending / "rejected" / name)
    assert pending_status(pending, name) == "rejected"


def test_missing_pending_sql_is_requeued(tmp_path: Path):
    assert pending_status(tmp_path, "20261111T043000_discovery_pages_0.sql") == "missing"


def test_sql_stage_resumes_only_after_application(tmp_path: Path):
    batch = tmp_path / "batch"
    batch.mkdir()
    sql = batch / "apply.sql"
    sql.write_text("DO $$ BEGIN NULL; END $$;")
    pending = tmp_path / "pending_sql"
    name = "20261111T043000_discovery_pages_0.sql"
    state = {"stage": "pages_wait", "pending": name, "sql": str(sql), "attempt": 0}
    assert not settle_sql(batch, state, pending)
    assert (pending / name).read_text() == sql.read_text()
    assert not settle_sql(batch, state, pending)
    (pending / "applied").mkdir()
    (pending / name).rename(pending / "applied" / name)
    assert settle_sql(batch, state, pending)
    assert json.loads((batch / "state.json").read_text())["stage"] == "offers"


def test_rejected_sql_replans_without_advancing(tmp_path: Path):
    pending = tmp_path / "pending_sql"
    (pending / "rejected").mkdir(parents=True)
    name = "20261111T043000_discovery_pages_0.sql"
    (pending / "rejected" / name).write_text("DO $$ BEGIN NULL; END $$;")
    state = {"stage": "pages_wait", "pending": name, "attempt": 0}
    assert settle_sql(tmp_path, state, pending)
    assert json.loads((tmp_path / "state.json").read_text())["stage"] == "pages"
    assert state["attempt"] == 1


def test_empty_discovery_completes_without_sql(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (tmp_path / "state.json").write_text(json.dumps({"stage": "crawl", "business_ids": [1]}))
    (pages / "decisions.json").write_text(json.dumps({"decisions": []}))
    (pages / "discovered.json").write_text(json.dumps({"summary": {"unfinished": 0, "scrape_failed": 0}}))
    assert run_batch(tmp_path) == "done"
    assert json.loads((tmp_path / "state.json").read_text())["stage"] == "done"


def test_sql_recovery_across_processes(tmp_path: Path):
    sql = tmp_path / "apply.sql"
    sql.write_text("DO $$ BEGIN NULL; END $$;")
    pending = tmp_path / "pending_sql"
    name = "20261111T043000_discovery_pages_0.sql"
    state = {"stage": "pages_wait", "pending": name, "sql": str(sql), "attempt": 0}
    (tmp_path / "state.json").write_text(json.dumps(state))
    code = ("import json,sys; from pathlib import Path; from utils.scheduled_discovery import settle_sql; "
            "b=Path(sys.argv[1]); print(settle_sql(b,json.loads((b/'state.json').read_text()),Path(sys.argv[2])))")
    def resume() -> str:
        return subprocess.check_output([sys.executable, "-c", code, str(tmp_path), str(pending)], text=True).strip()
    assert resume() == "False"
    assert resume() == "False"
    (pending / "applied").mkdir()
    (pending / name).rename(pending / "applied" / name)
    assert resume() == "True"
    assert json.loads((tmp_path / "state.json").read_text())["stage"] == "offers"


def test_city_check_waits_until_0430_and_limits_daily_batches():
    pacific = ZoneInfo("America/Los_Angeles")
    assert not city_check_due([], datetime(2026, 11, 11, 3, 30, tzinfo=pacific))
    assert city_check_due([], datetime(2026, 11, 11, 4, 30, tzinfo=pacific))
    batch = Path("20261111T123000000000Z")
    assert city_check_due([batch], datetime(2026, 11, 11, 5, 30, tzinfo=pacific))
    batches = [Path(f"20261111T12300{i}000000Z") for i in range(5)]
    assert not city_check_due(batches, datetime(2026, 11, 11, 5, 30, tzinfo=pacific))
    assert city_check_due([batch], datetime(2026, 11, 12, 4, 30, tzinfo=pacific))
