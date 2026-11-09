from __future__ import annotations

import json
import time

import pytest

from scripts import run_data_quality_audit as audit


def _queue(count: int) -> dict:
    return {"entries": [
        {"source_kind": "service", "status": "pending", "source_url": f"https://e{i}.com/p", "business_id": i}
        for i in range(count)
    ]}


@pytest.fixture()
def no_browser(monkeypatch):
    import utils.monitor_rendered_browser as browser
    import utils.offer_extraction_llm as llm

    calls: list[str] = []

    def fake_render(url, directory, **kwargs):
        calls.append(url)
        raise RuntimeError("source_inaccessible_or_redirected")

    monkeypatch.setattr(browser, "render_page", fake_render)
    monkeypatch.setattr(llm, "build_client_from_env", lambda: object())
    return calls


def test_expired_deadline_defers_every_entry_without_rendering(tmp_path, no_browser) -> None:
    queue = _queue(3)
    deferred = audit.recheck_catalog_and_memberships(
        queue, {"memberships": [], "services": []}, tmp_path, deadline=time.monotonic() - 1
    )
    assert deferred == 3 and no_browser == []
    assert all(entry["status"] == "pending" for entry in queue["entries"])
    saved = json.loads((tmp_path / "data_quality" / "recheck_queue.json").read_text())
    assert len(saved["entries"]) == 3


def test_no_deadline_processes_all_entries(tmp_path, no_browser) -> None:
    queue = _queue(2)
    deferred = audit.recheck_catalog_and_memberships(queue, {"memberships": [], "services": []}, tmp_path)
    assert deferred == 0 and len(no_browser) == 2
    # 失败的复查保持 pending，按指数退避在之后的运行中自动重试，没有永久暂停
    assert all(entry["status"] == "pending" and entry["attempts"] == 1 and entry["retry_after"]
               for entry in queue["entries"])
    audit.recheck_catalog_and_memberships(queue, {"memberships": [], "services": []}, tmp_path)
    assert len(no_browser) == 2


def test_apply_reads_the_snapshot_itself_and_needs_no_approved_plan(tmp_path, monkeypatch, capsys) -> None:
    from utils.data_quality_writer import SNAPSHOT_TABLES
    from utils.pipeline_db import write_text

    reads = []

    def fake_fresh_snapshot(directory, tables):
        # 代替 Supabase CLI：按 read_snapshot 的格式写出空快照
        reads.append(sorted(tables))
        return write_text(directory / "snapshot.json",
                          json.dumps({name: {"count": 0, "rows": []} for name in tables}))

    monkeypatch.setattr(audit, "fresh_snapshot", fake_fresh_snapshot)
    monkeypatch.setattr("sys.argv", ["quality", "--state-dir", str(tmp_path), "--apply"])
    audit.main()
    assert reads == [sorted(SNAPSHOT_TABLES)]
    assert json.loads(capsys.readouterr().out)["issues"] == 0


def test_backoff_doubles_and_is_capped():
    from datetime import datetime, timezone
    at = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert audit.retry_after(1, at) == "2026-11-03T00:00:00+00:00"
    assert audit.retry_after(10, at) == "2026-12-01T00:00:00+00:00"
