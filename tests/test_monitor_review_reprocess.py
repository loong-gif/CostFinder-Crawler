from __future__ import annotations

import json
from datetime import datetime, timezone

import utils.monitor_health as monitor_health
from monitor_health_support import monitor_services, serve  # noqa: F401
from scripts import run_monitor_price_sync as runner
from utils.monitor_price_retry import RetryLimits
from utils.monitor_price_store import atomic_json

URL = "https://a.test/specials"
SNAPSHOT = {"promotions": [], "offers": [], "items": [], "services": [], "terms": [], "memberships": [],
            "businesses": [{"business_id": 1, "website": "a.test"}], "confirmations": [], "audit": [],
            "service_names": ["Botox", "Others"]}


def daily_run(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    atomic_json(config, {"schema_version": 1, "monitors": {"daily": {"targets": [{"urls": [URL]}]}}})

    def run_monitor(config, name, *, urls_filter, **kwargs):
        return {"monitor_name": name, "error": 1, "total_pages": 1, "successful_urls": [],
                "page_errors": [{"url": URL, "error": "ego_scrape_failed"}]}

    monkeypatch.setattr(runner, "run_monitor", run_monitor)
    monkeypatch.setattr(runner, "database_snapshot", lambda: SNAPSHOT)
    monkeypatch.setattr("sys.argv", ["sync", "--state-dir", str(tmp_path), "--config", str(config), "--all-pages"])
    assert runner.main() == 2
    return json.loads((tmp_path / "sync_report.json").read_text())["remediation"]


def test_daily_run_with_a_failing_service_reports_an_infrastructure_fault(tmp_path, monkeypatch, monitor_services):
    broken, broken_url = serve({"/health": (503, b"{}")})
    monkeypatch.setattr(monitor_health, "OCR_URL", broken_url)
    remediation = daily_run(tmp_path, monkeypatch)
    broken.shutdown()
    assert remediation["infra_faults"] == [{"service": "ocr", "error": "HTTP 503"}]
    assert remediation["site_failures"] == []
    assert json.loads((tmp_path / "remediation_log.jsonl").read_text())["action"] == "infra_fault"


def test_source_paused_before_the_run_keeps_counting_site_failure_days(tmp_path, monkeypatch, monitor_services):
    limits = RetryLimits(tmp_path)
    for _ in range(3):
        limits.failed("processing", URL, "page_not_accessible")
    # 本轮唯一抓取的页面失败：它在抓取前已暂停，不能算作原本正常的来源批量失败
    remediation = daily_run(tmp_path, monkeypatch)
    assert remediation["infra_faults"] == []
    assert remediation["site_failures"] == [{"url": URL, "days": 1, "reason": "ego_scrape_failed"}]


def evidence(root, name, markdown):
    path = root / "evidence" / f"{name}.json"
    atomic_json(path, {"url": URL, "captured_at": datetime.now(timezone.utc).isoformat(), "markdown": markdown})
    return path


def test_review_event_is_reprocessed_when_its_source_is_captured_on_schedule(tmp_path, monkeypatch):
    first = evidence(tmp_path, "old", "Botox $12")
    path = tmp_path / "events/1.json"
    atomic_json(path, {"event_id": "1", "status": "pending", "url": URL, "artifact_path": str(first),
                       "created_at": datetime.now(timezone.utc).isoformat()})
    renders = []
    final = {"url": "https://booking.test/x"}
    monkeypatch.setattr(runner, "render_page",
                        lambda url, *a, **k: renders.append(url) or {"accessible": True, "final_url": final["url"]})
    seen = []
    monkeypatch.setattr(runner, "extract_evidence", lambda artifact, *a: seen.append(artifact["markdown"]) or {})
    monkeypatch.setattr(runner, "build_plan", lambda *a: {"status": "ready", "operations": [], "review": []})
    report = runner.process_events(tmp_path, SNAPSHOT)
    row, = report["sources"]
    assert row["status"] == "review" and row["review"] == [{"reason": "source_redirect_requires_review"}]
    assert row["plan_path"] == str(tmp_path / "sync/1/plan.json")
    evidence(tmp_path, "fresh", "Botox $10")
    final["url"] = URL
    report = runner.process_events(tmp_path, SNAPSHOT, retry_probe_urls={URL})
    assert len(renders) == 2 and seen == ["Botox $10"]
    # 新证据跳回原站：跨域跳转的审查结论不再成立
    assert "source_redirect_requires_review" not in json.dumps(report["sources"])
    assert json.loads(path.read_text())["artifact_path"] == str(tmp_path / "evidence/fresh.json")
