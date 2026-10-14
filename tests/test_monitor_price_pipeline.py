from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.firecrawl_self_hosted_monitor import run_monitor
from scripts.run_monitor_price_sync import (bind_source, enqueue_expired_campaign_checks,
                                           prepare_event, process_events,
                                           retry_failed_scrape_urls, snapshot_business_graph,
                                           verified_batch_results)
from utils.monitor_price_extract import extract_evidence
from utils.monitor_price_store import atomic_json, manual_protections, run_lock
from utils.monitor_rendered_browser import resources_complete


def test_empty_response_and_retry_payloads_saved_without_event_or_snapshot_change(tmp_path,monkeypatch):
    from scripts.firecrawl_self_hosted_monitor import ScrapeFailure
    original={"pages":{"https://a.test":{"facts":["offer $10"]}}}
    atomic_json(tmp_path / "daily.json",original)
    class Monitor:
        calls=0
        def __init__(self,*args): pass
        def scrape_document(self,url):
            Monitor.calls+=1
            raise ScrapeFailure("firecrawl_empty_markdown",{"success":True,"data":{"markdown":""},"attempt":Monitor.calls})
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor",Monitor)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.time.sleep",lambda _:None)
    report=run_monitor({"monitors":{"daily":{"targets":[{"urls":["https://a.test"]}]}}},"daily",api_url="x",
                       api_key="",state_dir=tmp_path,timeout=1,limit=None,workers=1,send_notifications=False,catchup=True)
    assert report["error"] == 1
    assert json.loads((tmp_path / "daily.json").read_text())["pages"] == original["pages"]
    assert not list((tmp_path / "events").glob("*.json"))
    captured=json.loads(next((tmp_path / "failed_evidence").glob("*.json")).read_text())
    assert [attempt["raw_response"]["attempt"] for attempt in captured["failed_attempts"]] == [1,2]


def test_hourly_scrape_retry_selects_only_failures_newer_than_success(tmp_path):
    atomic_json(tmp_path / "failed_evidence" / "a.json", {
        "url":"https://a.test","captured_at":"2026-10-11T01:00:00+00:00"})
    atomic_json(tmp_path / "evidence" / "a.json", {
        "url":"https://a.test","captured_at":"2026-10-11T00:00:00+00:00"})
    atomic_json(tmp_path / "failed_evidence" / "b.json", {
        "url":"https://b.test","captured_at":"2026-10-11T00:00:00+00:00"})
    atomic_json(tmp_path / "evidence" / "b.json", {
        "url":"https://b.test","captured_at":"2026-10-11T01:00:00+00:00"})
    config={"monitors":{"daily":{"targets":[{"urls":["https://a.test","https://b.test","https://c.test"]}]}}}
    assert retry_failed_scrape_urls(tmp_path,config) == {"https://a.test"}


def test_hourly_retry_fetches_new_evidence_for_retired_unstarted_plan(tmp_path):
    atomic_json(tmp_path / "events/1.json",{"event_id":"1","status":"retryable",
                "url":"https://a.test","retry_reason":"fresh_scrape_required"})
    config={"monitors":{"daily":{"targets":[{"urls":["https://a.test"]}]}}}
    assert retry_failed_scrape_urls(tmp_path,config) == {"https://a.test"}


def test_replanning_retired_journal_requires_new_scrape_not_fresh_prepared_timestamp(tmp_path,monkeypatch):
    url="https://a.test/specials";old=tmp_path/"evidence/old.json";event_path=tmp_path/"events/1.json"
    old_time=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
    atomic_json(old,{"url":url,"captured_at":old_time,"markdown":"old quote"})
    atomic_json(event_path,{"event_id":"1","url":url,"artifact_path":str(old),"status":"retryable",
                "retry_reason":"fresh_scrape_required","fresh_evidence_after":old_time})
    snapshot={"promotions":[],"services":[],"businesses":[{"business_id":1,"website":"a.test"}]}
    calls=[]
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_page",lambda *a:calls.append("browser") or {"accessible":True,"final_url":url})
    monkeypatch.setattr("scripts.run_monitor_price_sync.extract_evidence",lambda artifact,*a:calls.append(artifact["markdown"]) or {})
    monkeypatch.setattr("scripts.run_monitor_price_sync.build_plan",lambda *a:{"status":"ready","operations":[],"review":[]})
    plan=prepare_event(event_path,snapshot,{},tmp_path)
    assert plan["review"] == [{"reason":"fresh_scrape_required"}] and not calls
    assert json.loads(event_path.read_text())["status"] == "retryable"
    fresh=tmp_path/"evidence/new.json"
    atomic_json(fresh,{"url":url,"captured_at":datetime.now(timezone.utc).isoformat(),"markdown":"fresh quote"})
    snapshot.update(offers=[],items=[],terms=[])
    plan=prepare_event(event_path,snapshot,{},tmp_path)
    assert plan["status"] == "ready" and calls == ["browser","fresh quote"]
    assert json.loads(event_path.read_text())["artifact_path"] == str(fresh)


def test_expired_campaign_queues_one_check_only_after_fresh_scrape(tmp_path):
    root=tmp_path
    artifact=root/"evidence"/"fresh.json"
    atomic_json(artifact,{"url":"https://clinic.example/specials",
                          "captured_at":datetime.now(timezone.utc).isoformat(),"markdown":"Specials"})
    config={"monitors":{"daily":{"targets":[{"urls":["https://clinic.example/specials"]}]}}}
    snapshot={"promotions":[{"promotion_id":3,"source_url":"https://clinic.example/specials",
                              "is_active":True,"campaign_end_date":(date.today()-timedelta(days=1)).isoformat()}]}
    assert enqueue_expired_campaign_checks(root,snapshot,config) == 1
    assert enqueue_expired_campaign_checks(root,snapshot,config) == 0
    event=json.loads(next((root/"events").glob("*.json")).read_text())
    assert event["change_type"] == "campaign_expiry_check"
    assert event["artifact_path"] == str(artifact)


def test_failed_retry_then_success_history_survives_unchanged_next_capture(tmp_path,monkeypatch):
    from scripts.firecrawl_self_hosted_monitor import ScrapeFailure
    class Monitor:
        calls=0
        def __init__(self,*args): pass
        def scrape_document(self,url):
            Monitor.calls+=1
            if Monitor.calls==1:
                raise ScrapeFailure("firecrawl_empty_markdown",{"attempt":1,"markdown":""})
            return {"success":True,"data":{"markdown":"Botox special $10/unit"},"attempt":Monitor.calls}
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor",Monitor)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.time.sleep",lambda _:None)
    for _ in range(2):
        run_monitor({"monitors":{"daily":{"targets":[{"urls":["https://a.test"]}]}}},"daily",api_url="x",
                    api_key="",state_dir=tmp_path,timeout=1,limit=None,workers=1,send_notifications=False,catchup=True)
    failure=json.loads(next((tmp_path / "failed_evidence").glob("*.json")).read_text())
    assert failure["failed_attempts"][0]["raw_response"]["attempt"] == 1
    successes=[json.loads(f.read_text())["raw_response"]["attempt"] for f in (tmp_path / "evidence").glob("*.json")]
    assert sorted(successes) == [2,3]
    assert len(list((tmp_path / "events").glob("*.json"))) == 1


def test_partial_baseline_does_not_hide_later_page_changes(tmp_path, monkeypatch):
    atomic_json(tmp_path / "daily.json", {"baseline":True,"baseline_complete":False,
                 "pages":{"https://a.test":{"facts":["botox special $10/unit"]}}})
    class Monitor:
        def __init__(self,*args): pass
        def scrape(self,url):
            if "bad" in url: raise ValueError("failed")
            return "Botox special $12/unit"
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor",Monitor)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.time.sleep",lambda _:None)
    config={"monitors":{"daily":{"targets":[{"urls":["https://a.test","https://bad.test"]}]}}}
    result=run_monitor(config,"daily",api_url="x",api_key="",state_dir=tmp_path,
                       timeout=1,limit=None,workers=1,send_notifications=False,catchup=True)
    assert result["meaningful_events"] == 1
    assert result["error"] == 1
    assert len(list((tmp_path / "events").glob("*.json"))) == 1


def test_event_persistence_failure_does_not_advance_snapshot(tmp_path,monkeypatch):
    original={"pages":{"https://a.test":{"facts":["offer $10"]}}}
    atomic_json(tmp_path / "daily.json", original)
    class Monitor:
        def __init__(self,*args): pass
        def scrape(self,url): return "Botox special $12/unit"
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor",Monitor)
    def fail(path,value):
        if path.parent.name == "events": raise OSError("disk full")
        atomic_json(path,value)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.atomic_json",fail)
    with pytest.raises(OSError):
        run_monitor({"monitors":{"daily":{"targets":[{"urls":["https://a.test"]}]}}},"daily",
                    api_url="x",api_key="",state_dir=tmp_path,timeout=1,limit=None,workers=1,
                    send_notifications=False,catchup=True)
    assert json.loads((tmp_path / "daily.json").read_text()) == original


def test_overlapping_run_rejected(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(RuntimeError,match="already_running"):
            with run_lock(tmp_path): pass


def test_protections_keep_admin_and_business_decisions():
    result=manual_protections({"confirmations":[{"id":"x","offer_id":8,"kind":"correction","status":"resolved"},
        {"id":"y","offer_id":9,"kind":"confirmed","status":"dismissed"}],
        "audit":[{"id":"a","target_type":"deal","target_id":"10","action":"deal_delisted"}]})
    assert {x["id"] for x in result["entries"]} == {8,10}
    assert manual_protections({"confirmations":[],"audit":[]},result)["entries"] == result["entries"]


def test_source_binding_does_not_choose_between_branches():
    snap={"promotions":[],"services":[],"businesses":[{"business_id":1,"website":"a.test"},{"business_id":2,"website":"a.test"}]}
    with pytest.raises(RuntimeError,match="ambiguous"):
        bind_source("https://a.test/specials",snap)


@pytest.mark.parametrize("url,memberships",[
    ("https://omahapmc.com/specials/black-diamond-botox-membership", []),
    ("https://a.test/pages/specials-memberships", []),
    ("https://a.test/specials", [{"plan_id":1,"business_id":1,"source_url":"http://www.a.test/specials/"}]),
])
def test_membership_and_mixed_sources_require_review(url,memberships):
    snapshot={"promotions":[{"business_id":1,"source_url":url}],"services":[],
              "businesses":[],"memberships":memberships}
    with pytest.raises(RuntimeError,match="membership_source_requires_review"):
        bind_source(url,snapshot)


@pytest.mark.parametrize("url,final_url",[
    ("https://a.test/botox-membership", "https://a.test/botox-membership"),
    ("https://a.test/specials", "https://a.test/memberships"),
])
def test_cached_ready_membership_plan_is_replaced_with_review(tmp_path,monkeypatch,url,final_url):
    path=tmp_path / "events/1.json"
    atomic_json(path,{"event_id":"1","status":"planned","url":url})
    directory=tmp_path / "sync/1"
    cached={"status":"ready","prepared_at":"2099-01-01T00:00:00+00:00",
            "operations":[{"table":"clinic_promotions","action":"insert"}]}
    atomic_json(directory / "plan.json",cached)
    atomic_json(directory / "browser.json",{"final_url":final_url})
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_page",lambda *a:pytest.fail("cached source must stop before rendering"))
    plan=prepare_event(path,{"promotions":[],"services":[],"businesses":[{"business_id":1,"website":"a.test"}]},{},tmp_path)
    assert plan["status"] == "review" and plan["operations"] == []
    assert plan["review"] == [{"reason":"membership_source_requires_review"}]
    assert json.loads((directory / "plan_attempt_1.json").read_text()) == cached
    assert json.loads(path.read_text())["status"] == "review"


def test_live_same_domain_membership_redirect_stops_before_extraction(tmp_path,monkeypatch):
    path=tmp_path / "events/1.json"
    artifact=tmp_path / "artifact.json"
    atomic_json(artifact,{"url":"https://a.test/specials","markdown":"Botox price $10"})
    atomic_json(path,{"event_id":"1","status":"pending","url":"https://a.test/specials","artifact_path":str(artifact)})
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_page",lambda *a:{"accessible":True,"final_url":"https://a.test/membership"})
    monkeypatch.setattr("scripts.run_monitor_price_sync.extract_evidence",lambda *a:pytest.fail("membership must not extract"))
    snapshot={"promotions":[],"services":[],"offers":[],"items":[],"businesses":[{"business_id":1,"website":"a.test"}]}
    plan=prepare_event(path,snapshot,{},tmp_path)
    assert plan["status"] == "review" and plan["operations"] == []
    assert plan["review"] == [{"reason":"membership_source_requires_review"}]


@pytest.mark.parametrize("code,reason,status",[
    (402,"extraction_quota_exhausted","retryable"),
    (429,"extraction_rate_limited","retryable"),
    (401,"extraction_credentials_rejected","review"),
])
def test_extraction_http_failures_preserve_queue_with_safe_categories(tmp_path,monkeypatch,code,reason,status):
    from requests import HTTPError,Response
    path=tmp_path / "events/1.json"
    artifact=tmp_path / "artifact.json"
    atomic_json(artifact,{"url":"https://a.test/specials","markdown":"Botox price $10"})
    atomic_json(path,{"event_id":"1","status":"pending","url":"https://a.test/specials","artifact_path":str(artifact)})
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_page",lambda *a:{"accessible":True,"final_url":"https://a.test/specials"})
    def fail(*args):
        response=Response();response.status_code=code
        raise HTTPError("do not persist credential-bearing HTTP exception text",response=response)
    monkeypatch.setattr("scripts.run_monitor_price_sync.extract_evidence",fail)
    snapshot={"promotions":[],"services":[],"offers":[],"items":[],"businesses":[{"business_id":1,"website":"a.test"}]}
    plan=prepare_event(path,snapshot,{},tmp_path)
    assert plan["status"] == "review" and plan["operations"] == []
    assert plan["review"] == [{"reason":reason}]
    assert json.loads(path.read_text())["status"] == status


def test_business_snapshot_filters_services():
    graph=snapshot_business_graph({"promotions":[],"offers":[],"items":[],
        "services":[{"service_id":1,"business_id":1},{"service_id":2,"business_id":2}]},1)
    assert [s["service_id"] for s in graph["services"]] == [1]


class FakeLLM:
    def __init__(self,offers,empty=None): self.offers,self.empty=offers,empty
    def create_json_response(self,*args,**kwargs):
        return {"offers":self.offers,"all_offers_extracted":True,"no_current_offers_quote":self.empty}


def test_hidden_markdown_price_cannot_pass_visibility(tmp_path):
    evidence=extract_evidence({"url":"https://a.test","markdown":"Botox special $10/unit"},
        {"accessible":True,"complete":True,"text":"No current specials","image_review_required":False},tmp_path,
        FakeLLM([{"offer_raw_text":"Botox special $10/unit","items":[]}]))
    assert evidence["offers"][0]["visibility"] == "unverified"
    assert evidence["complete"] is False


def test_unverified_image_blocks_removal_but_text_claim_is_visible(tmp_path):
    evidence=extract_evidence({"url":"https://a.test","markdown":""},
        {"accessible":True,"complete":True,"text":"Botox special $10/unit","image_review_required":True},tmp_path,
        FakeLLM([{"offer_raw_text":"Botox special $10/unit","items":[]}]))
    assert evidence["offers"][0]["visibility"] == "visible_match"
    assert evidence["complete"] is False


def test_complete_empty_extract_does_not_require_explicit_no_specials(tmp_path):
    browser={"accessible":True,"complete":True,"text":"No current specials","image_review_required":False}
    assert extract_evidence({"url":"https://a.test"},browser,tmp_path,FakeLLM([]))["complete"]
    assert extract_evidence({"url":"https://a.test"},browser,tmp_path,FakeLLM([],"No current specials"))["complete"]


def test_latest_event_supersedes_old_price_before_any_write(tmp_path,monkeypatch):
    for i in (1,2):
        atomic_json(tmp_path / "events" / f"{i}.json", {"event_id":str(i),"status":"pending","url":"https://a.test", "created_at":str(i)})
    selected=[]
    def prepare(path,*args):
        selected.append(path.name)
        return {"status":"ready","operations":[],"review":[]}
    monkeypatch.setattr("scripts.run_monitor_price_sync.prepare_event",prepare)
    monkeypatch.setattr("scripts.run_monitor_price_sync.apply_plan",lambda *a:pytest.fail("writes disabled"))
    report=process_events(tmp_path,{"confirmations":[],"audit":[]},write_enabled=False)
    assert selected == ["2.json"]
    assert report["sources"][0]["status"] == "writes_disabled"
    assert json.loads((tmp_path / "events/1.json").read_text())["status"] == "superseded"


def test_failed_page_and_review_source_do_not_block_ready_source_write(tmp_path,monkeypatch):
    from scripts.run_monitor_price_sync import main
    class Monitor:
        def __init__(self,*args): pass
        def scrape(self,url):
            if "failed" in url:
                raise RuntimeError("page_failed")
            return "Botox special $12/unit"
    config=tmp_path / "config.json"
    atomic_json(config,{"schema_version":1,"monitors":{"daily":{"targets":[{"urls":["https://ready.test","https://failed.test"]}]}}})
    atomic_json(tmp_path / "price_sync_control.json",{"write_enabled":True})
    atomic_json(tmp_path / "events/review.json",{"event_id":"review","status":"review",
                "url":"https://review.test","created_at":"2026-10-09T00:00:00+00:00"})
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor",Monitor)
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.time.sleep",lambda _:None)
    monkeypatch.setattr("scripts.run_monitor_price_sync.database_snapshot",lambda:{"confirmations":[],"audit":[]})
    monkeypatch.setattr("scripts.run_monitor_price_sync.enqueue_expired_campaign_checks",lambda *a:0)
    monkeypatch.setattr("scripts.run_monitor_price_sync.target_client",lambda:object())
    monkeypatch.setattr("scripts.run_monitor_price_sync.prepare_event",lambda path,*a:
        {"status":"review","operations":[],"review":[{"reason":"unknown_service"}]} if path.stem=="review"
        else {"status":"ready","operations":[{"id":4,"values":{"discount_price":12}}],"review":[]})
    writes=[]
    def apply(plan,*a):
        writes.extend(plan["operations"])
        return {"status":"completed","category":"verified"}
    monkeypatch.setattr("scripts.run_monitor_price_sync.apply_plan",apply)
    monkeypatch.setattr("sys.argv",["sync","--state-dir",str(tmp_path),"--config",str(config)])
    assert main() == 2  # Failed page remains visible to the scheduler for retry.
    report=json.loads((tmp_path / "sync_report.json").read_text())
    assert writes == [{"id":4,"values":{"discount_price":12}}]
    assert report["write_enabled"] is True
    assert report["scrape_failed_pages"] == 1
    assert report["source_status_counts"] == {"completed":1,"review":1}
    assert report["failure_scope"] == "page_or_source_only"
    archived=next((tmp_path / "runs").glob("*.json"))
    assert json.loads(archived.read_text()) == report
    assert report["run_type"] == "full_monitor"
    assert not list((tmp_path / "journals").glob("*.json"))


def test_prior_verified_records_are_recognized_without_replaying_or_hiding_new_differences(tmp_path):
    receipt={"batch_id":"confirmed","url":"https://ready.test","verified_at":"2026-10-11T05:00:00+00:00",
             "records":[{"group":"offers","pk":"id","id":4,"values":{"discount_price":12,"is_active":True}}]}
    atomic_json(tmp_path / "verified_batch_receipts.json",[receipt])
    snapshot={"offers":[{"id":4,"discount_price":12,"is_active":True}]}
    result=verified_batch_results(tmp_path,snapshot)[0]
    assert result["status"] == "already_synchronized" and result["confirmed_records"] == [{"group":"offers","id":4}]
    snapshot["offers"][0]["discount_price"] = 13
    result=verified_batch_results(tmp_path,snapshot)[0]
    assert result["status"] == "changed_since_verification"
    assert result["changed_records"] == [{"group":"offers","id":4,"fields":["discount_price"]}]


def test_returning_price_change_is_not_deduplicated_forever(tmp_path,monkeypatch):
    class Monitor:
        price=10
        def __init__(self,*args):pass
        def scrape(self,url):return f"Botox special ${self.price}/unit"
    monkeypatch.setattr("scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor",Monitor)
    config={"monitors":{"daily":{"targets":[{"urls":["https://a.test"]}]}}}
    for price in (10,12,10,12):
        Monitor.price=price
        run_monitor(config,"daily",api_url="x",api_key="",state_dir=tmp_path,timeout=1,
                    limit=None,workers=1,send_notifications=False,catchup=True)
    assert len(list((tmp_path / "events").glob("*.json"))) == 4


@pytest.mark.parametrize("resource",["small_image","failed_background","retried_background"])
def test_failed_visual_resource_allows_text_update_but_never_removal(tmp_path,resource):
    from test_monitor_price_plan import SOURCE, _graph, _offer, _evidence
    from utils.monitor_price_plan import build_plan
    frame={"ready":True,"unreviewed_regions":0,"images":[],"backgrounds":[]}
    if resource == "small_image":
        frame["images"]=[{"width":600,"height":60,"loaded":False}]
    else:
        frame["backgrounds"]=[{"decoded":resource == "retried_background","network_failed":True}]
    assert not resources_complete([frame])
    graph=_graph()
    extra=dict(graph["offers"][0],id=9)
    graph["offers"].append(extra)
    # A distinct old image offer must remain active when visual resources fail.
    graph["items"].append(dict(graph["items"][0],offer_item_id=10,offer_id=9,service_id=13,item_name="Dysport"))
    graph["services"].append({"service_id":13,"business_id":7,"service_name":"Dysport","service_area":None})
    evidence=_evidence([_offer()],complete=resources_complete([frame]))
    plan=build_plan(SOURCE,graph,evidence,{})
    assert plan["status"] == "ready",plan
    assert not any(op["values"].get("is_active") is False for op in plan["operations"])
