from __future__ import annotations

import json

from scripts import notify_monitor_run
from utils.monitor_price_store import atomic_json
from utils.run_notification import report_message

STARTED = "2026-11-07T08:30:00+00:00"
REPORT = {
    "generated_at": "2026-11-07T09:12:00+00:00", "run_type": "full_monitor",
    "monitors": [{"total_pages": 18, "changed": 2, "error": 1,
                  "page_errors": [{"url": "https://down.test/specials", "error": "firecrawl_empty_markdown"}]}],
    "selected_events": 3, "source_status_counts": {"pending_sql": 1, "review": 2},
    "sources": [{"url": "https://a.test/specials", "status": "pending_sql"}],
    "expiry_checks_enqueued": 1, "quality_checks_enqueued": 0, "paused_retries": [],
}
TIERS = {"by_tier_days": {"7": 37, "15": 200, "30": 0, "60": 120, "90": 65}, "untracked": 17}


def test_summary_lists_captures_events_pending_sql_and_failed_pages():
    text = report_message(REPORT, started_at=STARTED, exit_code=2, tiers=TIERS)
    assert "【CostFinder 每日监控】2026-11-07 00:30 PT，用时 42 分钟，退出码 2" in text
    assert "抓取：18 页，成功 17，失败 1，价格变化 2" in text
    assert "7 天 37 页 / 15 天 200 页 / 30 天 0 页；会员页 60 天 120 页 / 90 天 65 页，未建档 17 页" in text
    assert "处理 3 个（pending_sql 1，review 2）" in text
    assert "待执行 SQL 1 个，需经 Supabase MCP 执行" in text and "https://a.test/specials" in text
    assert "写入开关" not in text
    assert "https://down.test/specials — firecrawl_empty_markdown" in text


def test_summary_reports_the_day_of_automatic_remediation():
    remediation = {"removed": ["https://gone.test/specials"], "restored": [], "recheck_failed": ["https://r.test/x"],
                   "awaiting_human": [{"url": f"https://h{n}.test/specials", "reasons": ["ambiguous_offer_match"]}
                                      for n in range(3)],
                   "resolved": ["https://fixed.test/specials"],
                   "infra_faults": [{"service": "ocr", "error": "HTTP 503"},
                                    {"reason": "page_not_accessible", "count": 40, "total": 120}],
                   "site_failures": [{"url": "https://b.test/specials", "days": 9, "reason": "ego_scrape_failed"},
                                     {"url": "https://c.test/specials", "days": 2, "reason": "page_not_accessible"}]}
    paused = [{"url": "https://i1.test", "retry_stage": "processing", "last_reason": "image_capture_incomplete",
               "first_failed_at": "2026-11-02T08:00:00+00:00"},
              {"url": "https://i2.test", "retry_stage": "processing", "last_reason": "image_capture_incomplete",
               "first_failed_at": "2026-11-06T08:00:00+00:00"},
              {"url": "https://n.test", "retry_stage": "scrape", "last_reason": "ConnectionError",
               "first_failed_at": "2026-11-07T08:00:00+00:00"},
              {"url": "https://s.test", "retry_stage": "scrape", "last_reason": "ego_scrape_failed",
               "first_failed_at": "2026-11-01T08:00:00+00:00"}]
    text = report_message({**REPORT, "remediation": remediation, "paused_retries": paused},
                          started_at=STARTED, exit_code=0, tiers=None)
    assert "自动处理：移出监控 1，恢复监控 0，复查未恢复 1，等待人工决定 3，已解决 1" in text
    assert "等待人工决定（列出 2/3）：" in text
    assert "  · https://h1.test/specials — ambiguous_offer_match" in text and "https://h2.test" not in text
    assert "基础设施故障（不计入来源天数）：ocr 健康检查失败 HTTP 503；page_not_accessible 40/120 页" in text
    assert "站点无法访问：2 个来源在计数，最长 9/14 天" in text
    assert "流水线问题（每日自动重试，不移出监控）：image_capture_incomplete 2 个（最长 5 天），ConnectionError 1 个（最长 0 天）" in text
    assert "暂停小时重试" not in text


def test_summary_without_remediation_has_no_remediation_lines():
    text = report_message(REPORT, started_at=STARTED, exit_code=0, tiers=None)
    assert "自动处理" not in text and "基础设施故障" not in text and "流水线问题" not in text


def run(tmp_path, monkeypatch, capsys, *, run_type, exit_code, report=None):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema_version": 1, "monitors": {"daily": {"targets": [{"urls": ["https://a.test"]}]}}}))
    (tmp_path / "monitor.log").write_text("line 1\nTraceback (most recent call last):\nKeyError: 'x'\n")
    if report is not None:
        atomic_json(tmp_path / "sync_report.json", report)
    monkeypatch.setattr("sys.argv", ["notify", "--state-dir", str(tmp_path), "--config", str(config),
                                     "--run-type", run_type, "--started-at", STARTED, "--exit-code", str(exit_code),
                                     "--log", str(tmp_path / "monitor.log"), "--dry-run"])
    assert notify_monitor_run.main() == 0
    return capsys.readouterr().out


def test_crash_without_a_fresh_report_sends_the_log_tail(tmp_path, monkeypatch, capsys):
    stale = {**REPORT, "generated_at": "2026-11-06T09:00:00+00:00"}
    out = run(tmp_path, monkeypatch, capsys, run_type="full_monitor", exit_code=1, report=stale)
    assert "运行异常终止，退出码 1" in out and "KeyError: 'x'" in out


def test_idle_hourly_retry_sends_nothing_and_a_busy_one_is_reported(tmp_path, monkeypatch, capsys):
    idle = {**REPORT, "run_type": "failure_retry", "monitors": [], "selected_events": 0}
    assert run(tmp_path, monkeypatch, capsys, run_type="failure_retry", exit_code=0,
               report=idle).strip() == "idle retry run: no notification"
    busy = {**REPORT, "run_type": "failure_retry"}
    assert "【CostFinder 失败重试】" in run(tmp_path, monkeypatch, capsys, run_type="failure_retry", exit_code=2,
                                           report=busy)
