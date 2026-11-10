from __future__ import annotations

import json

import pytest

from monitor_health_support import monitor_services  # noqa: F401
from scripts.run_monitor_price_sync import main, process_events
from utils.monitor_price_store import atomic_json


def capture_browser_unavailable(*args,**kwargs):
    # 抓取阶段的 ego-lite 渲染在测试中不可用：页面只有爬虫正文
    raise RuntimeError("rendered_browser_unavailable")


def test_latest_event_supersedes_old_price_before_any_write(tmp_path,monkeypatch):
    for i in (1,2):
        atomic_json(tmp_path / "events" / f"{i}.json", {"event_id":str(i),"status":"pending","url":"https://a.test", "created_at":str(i)})
    selected=[]
    def prepare(path,*args):
        selected.append(path.name)
        return {"status":"ready","operations":[],"review":[]}
    monkeypatch.setattr("scripts.run_monitor_price_sync.prepare_event",prepare)
    rendered=[]
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_pending",
                        lambda plan,*a:rendered.append(plan) or {"status":"pending_sql","sql_path":"x.sql"})
    report=process_events(tmp_path,{"confirmations":[],"audit":[]})
    assert selected == ["2.json"] and len(rendered) == 1
    assert report["sources"][0]["status"] == "pending_sql"
    assert json.loads((tmp_path / "events/1.json").read_text())["status"] == "superseded"


def test_capture_only_run_records_captures_without_planning_or_pending_sql(tmp_path,monkeypatch):
    class Monitor:
        def __init__(self,*args): pass
        def scrape(self,url): return "Botox special $12/unit"
    config=tmp_path / "config.json"
    atomic_json(config,{"schema_version":1,"monitors":{"due_01":{"targets":[{"urls":["https://a.test/specials"]}]}}})
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor",Monitor)
    monkeypatch.setattr("utils.monitor_rendered_browser.render_page",capture_browser_unavailable)
    monkeypatch.setattr("scripts.run_monitor_price_sync.database_snapshot",lambda:{"confirmations":[],"audit":[]})
    monkeypatch.setattr("scripts.run_monitor_price_sync.prepare_event",lambda *a:pytest.fail("capture-only run plans nothing"))
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_pending",lambda *a:pytest.fail("capture-only run renders no SQL"))
    monkeypatch.setattr("sys.argv",["sync","--state-dir",str(tmp_path),"--config",str(config),"--all-pages","--capture-only"])
    assert main() == 0
    report=json.loads((tmp_path / "monitor_run_report.json").read_text())
    assert report["reports"][0]["successful_urls"] == ["https://a.test/specials"]
    assert not (tmp_path / "pending_sql").exists() and not (tmp_path / "sync_report.json").exists()


def test_membership_source_is_excluded_from_promotion_monitoring_and_never_selected_again(tmp_path,monkeypatch):
    url="https://a.test/specials/botox-membership"
    atomic_json(tmp_path / "events/1.json",{"event_id":"1","status":"pending","url":url,"created_at":"1"})
    snapshot={"promotions":[],"services":[],"businesses":[{"business_id":1,"website":"a.test"}],
              "memberships":[],"confirmations":[],"audit":[]}
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_page",lambda *a,**k:pytest.fail("membership source must not render"))
    report=process_events(tmp_path,snapshot)
    assert report["sources"][0]["status"] == "excluded"
    assert json.loads((tmp_path / "events/1.json").read_text())["status"] == "excluded"
    assert process_events(tmp_path,snapshot)["selected_events"] == 0


def test_failed_page_and_review_source_do_not_block_ready_source_write(tmp_path,monkeypatch,monitor_services):
    class Monitor:
        def __init__(self,*args): pass
        def scrape(self,url):
            if "failed" in url:
                raise RuntimeError("page_failed")
            return "Botox special $12/unit"
    config=tmp_path / "config.json"
    atomic_json(config,{"schema_version":1,"monitors":{"daily":{"targets":[{"urls":["https://ready.test","https://failed.test"]}]}}})
    atomic_json(tmp_path / "events/review.json",{"event_id":"review","status":"review",
                "url":"https://review.test","created_at":"2026-10-09T00:00:00+00:00"})
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor",Monitor)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.time.sleep",lambda _:None)
    monkeypatch.setattr("utils.monitor_rendered_browser.render_page",capture_browser_unavailable)
    monkeypatch.setattr("scripts.run_monitor_price_sync.database_snapshot",lambda:{"confirmations":[],"audit":[]})
    monkeypatch.setattr("scripts.run_monitor_price_sync.enqueue_expired_campaign_checks",lambda *a:0)
    monkeypatch.setattr("scripts.run_monitor_price_sync.prepare_event",lambda path,*a:
        {"status":"review","operations":[],"review":[{"reason":"unknown_service"}]} if path.stem=="review"
        else {"status":"ready","operations":[{"id":4,"values":{"discount_price":12}}],"review":[]})
    writes=[]
    def render(plan,protections,state_dir):
        writes.extend(plan["operations"])
        return {"status":"pending_sql","sql_path":str(state_dir / "pending_sql" / "x.sql")}
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_pending",render)
    monkeypatch.setattr("sys.argv",["sync","--state-dir",str(tmp_path),"--config",str(config)])
    assert main() == 2  # Failed page remains visible to the scheduler for retry.
    report=json.loads((tmp_path / "sync_report.json").read_text())
    assert writes == [{"id":4,"values":{"discount_price":12}}]
    assert "write_enabled" not in report
    assert report["scrape_failed_pages"] == 1
    assert report["source_status_counts"] == {"pending_sql":1,"review":1}
    assert report["failure_scope"] == "page_or_source_only"
    archived=next((tmp_path / "runs").glob("*.json"))
    assert json.loads(archived.read_text()) == report
    assert report["run_type"] == "full_monitor"
    assert not (tmp_path / "journals").exists()
