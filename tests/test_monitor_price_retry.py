"""Failure limits through the actual monitor/event entry points; no network."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from scripts import run_monitor_price_sync as runner
from scripts.firecrawl_self_hosted_monitor import run_monitor
from utils.monitor_event_queue import retry_failed_scrape_urls
from utils.monitor_frequency import SCHEDULE_FILE, record_check
from utils.monitor_price_retry import RetryLimits
from utils.monitor_price_store import atomic_json

URL = "https://a.test/specials"
CONFIG = {"schema_version": 1, "monitors": {"daily": {"targets": [{"urls": [URL]}]}}}
SNAPSHOT = {"promotions": [], "offers": [], "items": [], "services": [],
            "businesses": [{"business_id": 1, "website": "a.test"}], "confirmations": [], "audit": [],
            "service_names": ["Botox", "Others"]}


def capture_browser_unavailable(*args, **kwargs):
    # 抓取阶段的 ego-lite 渲染在测试中不可用：页面只有爬虫正文
    raise RuntimeError("rendered_browser_unavailable")


def setup_event(root, event_id="1", url=URL):
    artifact = root / "evidence" / (event_id + ".json")
    atomic_json(artifact, {"url": url, "captured_at": datetime.now(timezone.utc).isoformat(),
                          "markdown": "Botox $12/unit"})
    path = root / "events" / (event_id + ".json")
    atomic_json(path, {"event_id": event_id, "status": "pending", "url": url,
                       "artifact_path": str(artifact), "created_at": datetime.now(timezone.utc).isoformat()})
    return path


def test_three_failed_runs_pause_hourly_fetch_and_weekly_success_resets(tmp_path, monkeypatch):
    class Monitor:
        failing = True
        calls = 0
        def __init__(self, *args): pass
        def scrape(self, url):
            Monitor.calls += 1
            if Monitor.failing:
                raise TimeoutError("page_timeout")
            return "Botox $12/unit"
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor", Monitor)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.time.sleep", lambda _: None)
    def capture():
        return run_monitor(CONFIG, "daily", api_url="x", api_key="", state_dir=tmp_path,
                           timeout=1, limit=None, workers=1, send_notifications=False, catchup=True)
    for count in range(1, 4):
        assert capture()["error"] == 1
        selected = retry_failed_scrape_urls(tmp_path, CONFIG)
        assert selected == ({URL} if count < 3 else set())
        assert RetryLimits(tmp_path).get("scrape", URL)["consecutive_failures"] == count
    assert Monitor.calls == 6  # Each failed page run still has two HTTP attempts.
    assert RetryLimits(tmp_path).paused_sources()[0]["hourly_retry_paused"] is True
    # The next weekly capture is not filtered by the hourly limit.
    assert capture()["error"] == 1
    assert retry_failed_scrape_urls(tmp_path, CONFIG) == set()
    Monitor.failing = False
    assert capture()["successful_urls"] == [URL]
    assert retry_failed_scrape_urls(tmp_path, CONFIG) == set()
    assert not RetryLimits(tmp_path).paused("scrape", URL)
    Monitor.failing = True
    capture()
    assert retry_failed_scrape_urls(tmp_path, CONFIG) == {URL}
    assert RetryLimits(tmp_path).get("scrape", URL)["consecutive_failures"] == 1


def test_existing_failure_history_and_fresh_evidence_event_cannot_bypass_scrape_cap(tmp_path):
    for n in range(3):
        atomic_json(tmp_path / "failed_evidence" / f"{n}.json",
                    {"url": URL, "captured_at": f"2026-10-11T0{n}:00:00+00:00", "error": "timeout"})
    atomic_json(tmp_path / "events/1.json", {"event_id": "1", "status": "retryable", "url": URL,
                                            "retry_reason": "fresh_scrape_required"})
    assert retry_failed_scrape_urls(tmp_path, CONFIG) == set()
    # URL spelling changes do not reset the same source's limit.
    alternate = {"monitors": {"daily": {"targets": [{"urls": ["http://www.a.test/specials/"]}]}}}
    assert retry_failed_scrape_urls(tmp_path, alternate) == set()
    assert RetryLimits(tmp_path).paused("scrape", "http://www.a.test/specials/")


def test_render_failure_limit_survives_new_events_and_weekly_fresh_success_resets(tmp_path, monkeypatch):
    path = setup_event(tmp_path)
    calls = []
    monkeypatch.setattr(runner, "render_page", lambda *a, **k: calls.append("browser") or {"accessible": False})
    for count in range(1, 4):
        runner.process_events(tmp_path, SNAPSHOT, retryable_only=count > 1)
        assert len(calls) == count
    assert RetryLimits(tmp_path).paused("processing", URL)
    assert json.loads(path.read_text())["status"] == "retryable"
    # A new event / URL variant must not silently reopen a paused source hourly.
    newer = setup_event(tmp_path, "2", "http://www.a.test/specials/")
    runner.process_events(tmp_path, SNAPSHOT)
    runner.process_events(tmp_path, SNAPSHOT, retryable_only=True)
    assert len(calls) == 3 and json.loads(newer.read_text())["status"] == "retryable"
    fresh = tmp_path / "evidence/fresh.json"
    atomic_json(fresh, {"url": URL, "captured_at": datetime.now(timezone.utc).isoformat(), "markdown": "fresh $12"})
    monkeypatch.setattr(runner, "render_page", lambda url, *a, **k: calls.append("browser") or {"accessible": True, "final_url": url})
    seen = []
    monkeypatch.setattr(runner, "extract_evidence", lambda artifact, *a: seen.append(artifact["markdown"]) or {})
    monkeypatch.setattr(runner, "build_plan", lambda *a: {"status": "ready", "operations": [], "review": []})
    runner.process_events(tmp_path, SNAPSHOT, retry_probe_urls={URL})
    assert len(calls) == 4 and seen == ["fresh $12"]
    assert not RetryLimits(tmp_path).paused("processing", URL)
    assert json.loads(newer.read_text())["artifact_path"] == str(fresh)


def test_failed_weekly_capture_does_not_reopen_processing_pause(tmp_path, monkeypatch):
    from scripts.firecrawl_self_hosted_monitor import ScrapeFailure
    limits = RetryLimits(tmp_path)
    for _ in range(3): limits.failed("processing", URL, "page_not_accessible")
    paused_path = setup_event(tmp_path)
    artifact_path = json.loads(paused_path.read_text())["artifact_path"]
    artifact = json.loads(Path(artifact_path).read_text())
    artifact["captured_at"] = "2026-10-10T00:00:00+00:00"
    atomic_json(Path(artifact_path), artifact)
    config = tmp_path / "config.json"; atomic_json(config, CONFIG)
    class Monitor:
        def __init__(self, *args): pass
        def scrape(self, url): raise ScrapeFailure("firecrawl_empty_markdown", {})
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor", Monitor)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.time.sleep", lambda _: None)
    monkeypatch.setattr(runner, "render_page", lambda *a, **k: pytest.fail("No successful weekly capture"))
    monkeypatch.setattr("utils.monitor_rendered_browser.render_page", capture_browser_unavailable)
    monkeypatch.setattr(runner, "database_snapshot", lambda: SNAPSHOT)
    monkeypatch.setattr("sys.argv", ["sync", "--state-dir", str(tmp_path), "--config", str(config)])
    assert runner.main() == 2
    assert RetryLimits(tmp_path).get("processing", URL)["consecutive_failures"] == 3


def test_hourly_main_skips_capped_page_but_writes_independent_ready_source(tmp_path, monkeypatch):
    ready = "https://ready.test/specials"
    for n in range(3):
        atomic_json(tmp_path / "failed_evidence" / f"{n}.json",
                    {"url": URL, "captured_at": f"2026-10-11T0{n}:00:00+00:00", "error": "timeout"})
    limits = RetryLimits(tmp_path)
    for _ in range(3): limits.failed("processing", URL, "page_not_accessible")
    paused_path = setup_event(tmp_path)
    artifact_path = Path(json.loads(paused_path.read_text())["artifact_path"])
    artifact = json.loads(artifact_path.read_text())
    artifact["captured_at"] = "2026-10-10T00:00:00+00:00"
    atomic_json(artifact_path, artifact)
    ready_path = setup_event(tmp_path, "ready", ready)
    event = json.loads(ready_path.read_text()); event["status"] = "retryable"; atomic_json(ready_path, event)
    config = tmp_path / "config.json"; atomic_json(config, CONFIG)
    monkeypatch.setattr(runner, "run_monitor", lambda *a, **k: pytest.fail("Capped page must not be scraped"))
    monkeypatch.setattr(runner, "database_snapshot", lambda: SNAPSHOT)
    monkeypatch.setattr(runner, "prepare_event", lambda path, *a: {"status": "ready", "operations": [], "review": []})
    writes = []
    monkeypatch.setattr(runner, "render_pending", lambda *a: writes.append("ready") or {"status": "pending_sql", "sql_path": "x.sql"})
    monkeypatch.setattr("sys.argv", ["sync", "--state-dir", str(tmp_path), "--config", str(config), "--retry-transient-only"])
    assert runner.main() == 0
    report = json.loads((tmp_path / "sync_report.json").read_text())
    assert writes == ["ready"]
    assert {r["retry_stage"] for r in report["paused_retries"]} == {"scrape", "processing"}
    # 暂停小时重试只是通知，每日任务继续抓取，不进审查清单
    assert json.loads((tmp_path / "review_queue.json").read_text()) == []


def pending_file_renderer(rendered):
    def render(plan, protections, state_dir):
        path = Path(state_dir) / "pending_sql" / f"{len(rendered)}_{plan['event_id']}.sql"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("-- plan\n")
        rendered.append(path)
        return {"status": "pending_sql", "sql_path": str(path)}
    return render


def test_pending_sql_event_waits_for_its_file_and_is_never_planned_or_rendered_twice(tmp_path, monkeypatch):
    path = setup_event(tmp_path)
    rendered = []
    monkeypatch.setattr(runner, "prepare_event", lambda p, *a: {"status": "ready", "operations": [], "review": [], "event_id": "1"})
    monkeypatch.setattr(runner, "render_pending", pending_file_renderer(rendered))
    runner.process_events(tmp_path, SNAPSHOT)
    assert json.loads(path.read_text())["status"] == "pending_sql" and len(rendered) == 1
    monkeypatch.setattr(runner, "prepare_event", lambda *a: pytest.fail("A pending SQL file owns this source event"))
    monkeypatch.setattr(runner, "render_pending", lambda *a: pytest.fail("Rendered twice"))
    for _ in range(3):
        report = runner.process_events(tmp_path, SNAPSHOT)
        assert report["selected_events"] == 0
    # The session executed the file and marked it applied: the event completes without another render.
    from utils.pending_sql import mark_applied
    mark_applied(rendered[0])
    runner.process_events(tmp_path, SNAPSHOT)
    event = json.loads(path.read_text())
    assert event["status"] == "completed" and event["receipt"]["sql_path"] == str(rendered[0])


def test_missing_pending_sql_file_reopens_the_event_for_one_new_render(tmp_path, monkeypatch):
    path = setup_event(tmp_path)
    rendered = []
    monkeypatch.setattr(runner, "prepare_event", lambda p, *a: {"status": "ready", "operations": [], "review": [], "event_id": "1"})
    monkeypatch.setattr(runner, "render_pending", pending_file_renderer(rendered))
    runner.process_events(tmp_path, SNAPSHOT)
    rendered[0].unlink()
    runner.process_events(tmp_path, SNAPSHOT)
    assert len(rendered) == 2 and json.loads(path.read_text())["status"] == "pending_sql"


def test_weekly_main_success_rechecks_paused_processing_with_new_artifact(tmp_path, monkeypatch):
    limits = RetryLimits(tmp_path)
    for _ in range(3): limits.failed("processing", URL, "page_not_accessible")
    event_path = setup_event(tmp_path)
    # 上次抓取已超过 7 天档期，今天的到期抓取会取得新正文
    atomic_json(tmp_path / SCHEDULE_FILE, {URL: record_check(
        None, at=datetime.now(timezone.utc) - timedelta(days=8), ok=True, changed=False)})
    config = tmp_path / "config.json"; atomic_json(config, CONFIG)
    class Monitor:
        def __init__(self, *args): pass
        def scrape(self, url): return "Fresh Botox $12/unit"
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor", Monitor)
    monkeypatch.setattr(runner, "database_snapshot", lambda: SNAPSHOT)
    monkeypatch.setattr(runner, "enqueue_expired_campaign_checks", lambda *a: 0)
    monkeypatch.setattr(runner, "render_page", lambda url, *a, **k: {"accessible": True, "final_url": url})
    monkeypatch.setattr("utils.monitor_rendered_browser.render_page", capture_browser_unavailable)
    seen = []
    monkeypatch.setattr(runner, "extract_evidence", lambda artifact, *a: seen.append(artifact["markdown"]) or {})
    monkeypatch.setattr(runner, "build_plan", lambda *a: {"status": "ready", "operations": [], "review": []})
    monkeypatch.setattr("sys.argv", ["sync", "--state-dir", str(tmp_path), "--config", str(config)])
    assert runner.main() == 0
    assert seen == ["Fresh Botox $12/unit"]
    assert not RetryLimits(tmp_path).paused("processing", URL)
    assert json.loads(event_path.read_text())["status"] == "superseded"


def test_processing_counts_for_parallel_sources_are_all_persisted(tmp_path, monkeypatch):
    snapshot = {**SNAPSHOT, "businesses": [{"business_id": n, "website": f"{n}.test"} for n in range(3)]}
    for n in range(3): setup_event(tmp_path, str(n), f"https://{n}.test/specials")
    monkeypatch.setattr(runner, "render_page", lambda *a, **k: {"accessible": False})
    runner.process_events(tmp_path, snapshot)
    assert len(RetryLimits(tmp_path).state["processing"]) == 3
    assert all(row["consecutive_failures"] == 1 for row in RetryLimits(tmp_path).state["processing"].values())
