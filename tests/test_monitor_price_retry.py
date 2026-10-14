"""Failure limits through the actual monitor/event entry points; no network."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from scripts import run_monitor_price_sync as runner
from scripts.firecrawl_self_hosted_monitor import run_monitor
from utils.monitor_price_retry import RetryLimits
from utils.monitor_price_store import atomic_json

URL = "https://a.test/specials"
CONFIG = {"schema_version": 1, "monitors": {"daily": {"targets": [{"urls": [URL]}]}}}
SNAPSHOT = {"promotions": [], "offers": [], "items": [], "services": [],
            "businesses": [{"business_id": 1, "website": "a.test"}], "confirmations": [], "audit": []}


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
        selected = runner.retry_failed_scrape_urls(tmp_path, CONFIG)
        assert selected == ({URL} if count < 3 else set())
        assert RetryLimits(tmp_path).get("scrape", URL)["consecutive_failures"] == count
    assert Monitor.calls == 6  # Each failed page run still has two HTTP attempts.
    assert RetryLimits(tmp_path).paused_reviews()[0]["category"] == "retry_limit_reached"
    # The next weekly capture is not filtered by the hourly limit.
    assert capture()["error"] == 1
    assert runner.retry_failed_scrape_urls(tmp_path, CONFIG) == set()
    Monitor.failing = False
    assert capture()["successful_urls"] == [URL]
    assert runner.retry_failed_scrape_urls(tmp_path, CONFIG) == set()
    assert not RetryLimits(tmp_path).paused("scrape", URL)
    Monitor.failing = True
    capture()
    assert runner.retry_failed_scrape_urls(tmp_path, CONFIG) == {URL}
    assert RetryLimits(tmp_path).get("scrape", URL)["consecutive_failures"] == 1


def test_existing_failure_history_and_fresh_evidence_event_cannot_bypass_scrape_cap(tmp_path):
    for n in range(3):
        atomic_json(tmp_path / "failed_evidence" / f"{n}.json",
                    {"url": URL, "captured_at": f"2026-10-11T0{n}:00:00+00:00", "error": "timeout"})
    atomic_json(tmp_path / "events/1.json", {"event_id": "1", "status": "retryable", "url": URL,
                                            "retry_reason": "fresh_scrape_required"})
    assert runner.retry_failed_scrape_urls(tmp_path, CONFIG) == set()
    # URL spelling changes do not reset the same source's limit.
    alternate = {"monitors": {"daily": {"targets": [{"urls": ["http://www.a.test/specials/"]}]}}}
    assert runner.retry_failed_scrape_urls(tmp_path, alternate) == set()
    assert RetryLimits(tmp_path).paused("scrape", "http://www.a.test/specials/")


def test_render_failure_limit_survives_new_events_and_weekly_fresh_success_resets(tmp_path, monkeypatch):
    path = setup_event(tmp_path)
    calls = []
    monkeypatch.setattr(runner, "render_page", lambda *a: calls.append("browser") or {"accessible": False})
    for count in range(1, 4):
        runner.process_events(tmp_path, SNAPSHOT, write_enabled=False, retryable_only=count > 1)
        assert len(calls) == count
    assert RetryLimits(tmp_path).paused("processing", URL)
    assert json.loads(path.read_text())["status"] == "review"
    # A new event / URL variant must not silently reopen a paused source hourly.
    newer = setup_event(tmp_path, "2", "http://www.a.test/specials/")
    runner.process_events(tmp_path, SNAPSHOT, write_enabled=False)
    runner.process_events(tmp_path, SNAPSHOT, write_enabled=False, retryable_only=True)
    assert len(calls) == 3 and json.loads(newer.read_text())["status"] == "review"
    fresh = tmp_path / "evidence/fresh.json"
    atomic_json(fresh, {"url": URL, "captured_at": datetime.now(timezone.utc).isoformat(), "markdown": "fresh $12"})
    monkeypatch.setattr(runner, "render_page", lambda url, *a: calls.append("browser") or {"accessible": True, "final_url": url})
    seen = []
    monkeypatch.setattr(runner, "extract_evidence", lambda artifact, *a: seen.append(artifact["markdown"]) or {})
    monkeypatch.setattr(runner, "build_plan", lambda *a: {"status": "ready", "operations": [], "review": []})
    runner.process_events(tmp_path, SNAPSHOT, write_enabled=False, retry_probe_urls={URL})
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
    monkeypatch.setattr(runner, "render_page", lambda *a: pytest.fail("No successful weekly capture"))
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
    atomic_json(tmp_path / "price_sync_control.json", {"write_enabled": True})
    monkeypatch.setattr(runner, "run_monitor", lambda *a, **k: pytest.fail("Capped page must not be scraped"))
    monkeypatch.setattr(runner, "database_snapshot", lambda: SNAPSHOT)
    monkeypatch.setattr(runner, "target_client", lambda: object())
    monkeypatch.setattr(runner, "prepare_event", lambda path, *a: {"status": "ready", "operations": [], "review": []})
    writes = []
    monkeypatch.setattr(runner, "apply_plan", lambda *a: writes.append("ready") or {"status": "completed"})
    monkeypatch.setattr("sys.argv", ["sync", "--state-dir", str(tmp_path), "--config", str(config), "--retry-transient-only"])
    assert runner.main() == 0
    report = json.loads((tmp_path / "sync_report.json").read_text())
    assert writes == ["ready"] and report["write_enabled"] is True
    assert {r["retry_stage"] for r in report["paused_retries"]} == {"scrape", "processing"}
    assert any(r.get("category") == "retry_limit_reached" for r in json.loads((tmp_path / "review_queue.json").read_text()))


def test_three_recovery_failures_pause_original_journal_then_weekly_resumes_same_plan(tmp_path, monkeypatch):
    path = setup_event(tmp_path)
    source = {"business_id": 1, "url": URL}
    plan = {"event_id": "1", "source": source, "prepared_at": datetime.now(timezone.utc).isoformat(),
            "allocated_ids": {"promo_offer_master": {"-1": 100}}}
    journal_path = tmp_path / "journals/original.json"
    journal = {"stage": "review", "transport": "rest", "plan": plan, "pending": {"op": {"action": "insert"}}}
    atomic_json(journal_path, journal)
    monkeypatch.setattr(runner, "target_client", lambda: object())
    monkeypatch.setattr(runner, "bind_source", lambda *a: source)
    calls, applied = [], []
    monkeypatch.setattr(runner, "recover_journal", lambda *a: calls.append("recover") or {"status": "review", "category": "readback_unavailable", "resume_required": True})
    monkeypatch.setattr(runner, "prepare_event", lambda *a: pytest.fail("Original journal owns this source"))
    for _ in range(4):
        runner.process_events(tmp_path, SNAPSHOT, write_enabled=True, retryable_only=True)
    assert len(calls) == 3 and RetryLimits(tmp_path).paused("recovery", URL)
    assert json.loads(journal_path.read_text()) == journal
    monkeypatch.setattr(runner, "recover_journal", lambda *a: calls.append("recover") or {"status": "retryable", "category": "verified_resume"})
    monkeypatch.setattr(runner, "apply_plan", lambda frozen, *a: applied.append(frozen) or {"status": "completed"})
    runner.process_events(tmp_path, SNAPSHOT, write_enabled=True, recovery_probe=True)
    assert len(calls) == 4 and applied == [plan]
    assert not RetryLimits(tmp_path).paused("recovery", URL)
    assert json.loads(path.read_text())["status"] == "completed"


def test_unknown_insert_is_never_reposted_by_retry_limit_or_weekly_probe(tmp_path, monkeypatch):
    source = {"business_id": 1, "url": URL}
    setup_event(tmp_path)
    atomic_json(tmp_path / "journals/unknown.json", {"stage": "review", "transport": "rest", "plan": {"source": source, "event_id": "1"}})
    monkeypatch.setattr(runner, "target_client", lambda: object())
    monkeypatch.setattr(runner, "bind_source", lambda *a: source)
    monkeypatch.setattr(runner, "recover_journal", lambda *a: {"status": "review", "category": "insert_outcome_unknown", "resume_required": True})
    monkeypatch.setattr(runner, "apply_plan", lambda *a: pytest.fail("Unknown POST cannot be repeated"))
    for _ in range(4): runner.process_events(tmp_path, SNAPSHOT, write_enabled=True, retryable_only=True)
    assert RetryLimits(tmp_path).paused("recovery", URL)
    runner.process_events(tmp_path, SNAPSHOT, write_enabled=True, recovery_probe=True)
    assert RetryLimits(tmp_path).get("recovery", URL)["last_reason"] == "insert_outcome_unknown"
    assert json.loads((tmp_path / "events/1.json").read_text())["receipt"]["category"] == "insert_outcome_unknown"


def test_weekly_main_success_rechecks_paused_processing_with_new_artifact(tmp_path, monkeypatch):
    limits = RetryLimits(tmp_path)
    for _ in range(3): limits.failed("processing", URL, "page_not_accessible")
    event_path = setup_event(tmp_path)
    config = tmp_path / "config.json"; atomic_json(config, CONFIG)
    class Monitor:
        def __init__(self, *args): pass
        def scrape(self, url): return "Fresh Botox $12/unit"
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor", Monitor)
    monkeypatch.setattr(runner, "database_snapshot", lambda: SNAPSHOT)
    monkeypatch.setattr(runner, "enqueue_expired_campaign_checks", lambda *a: 0)
    monkeypatch.setattr(runner, "render_page", lambda url, *a: {"accessible": True, "final_url": url})
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
    monkeypatch.setattr(runner, "render_page", lambda *a: {"accessible": False})
    runner.process_events(tmp_path, snapshot, write_enabled=False)
    assert len(RetryLimits(tmp_path).state["processing"]) == 3
    assert all(row["consecutive_failures"] == 1 for row in RetryLimits(tmp_path).state["processing"].values())
