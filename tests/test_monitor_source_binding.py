from __future__ import annotations

import json

import pytest

from scripts.run_monitor_price_sync import prepare_event
from utils.monitor_price_store import atomic_json
from utils.monitor_source_binding import bind_source, snapshot_business_graph

SERVICE_NAMES = ["Botox", "Dermal Filler", "Others"]


def test_source_binding_does_not_choose_between_businesses_it_cannot_tell_apart():
    # 地址缺失或相同：可能是重复登记，交人工审查
    for addresses in ([None, None], ["10 Main St, Austin, TX", "10 Main St., Austin TX"]):
        snap={"promotions":[],"services":[],"businesses":[
            {"business_id":1,"website":"a.test","address":addresses[0]},
            {"business_id":2,"website":"a.test","address":addresses[1]}]}
        with pytest.raises(RuntimeError,match="ambiguous"):
            bind_source("https://a.test/specials",snap)


def test_chain_branches_at_different_addresses_each_bind_to_the_shared_page():
    url="https://www.laseraway.com/promotions/"
    snap={"promotions":[{"business_id":4386,"source_url":"https://laseraway.com/promotions"},
                        {"business_id":4385,"source_url":"https://laseraway.com/promotions"}],
          "services":[],"businesses":[{"business_id":4385,"website":"laseraway.com","address":"8500 Melrose Ave, West Hollywood, CA"},
                                      {"business_id":4386,"website":"laseraway.com","address":"1 Grand Ave, Austin, TX"}]}
    assert bind_source(url,snap) == [{"business_id":4385,"url":url},{"business_id":4386,"url":url}]
    single={"promotions":[{"business_id":7,"source_url":"https://a.test/specials"}],"services":[],"businesses":[]}
    assert bind_source("https://a.test/specials",single) == [{"business_id":7,"url":"https://a.test/specials"}]


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
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_page",lambda *a, **k:pytest.fail("cached source must stop before rendering"))
    plan=prepare_event(path,{"promotions":[],"services":[],"businesses":[{"business_id":1,"website":"a.test"}]},{},tmp_path)
    assert plan["status"] == "review" and plan["operations"] == []
    assert plan["review"] == [{"reason":"membership_source_requires_review"}]
    assert json.loads((directory / "plan_attempt_1.json").read_text()) == cached
    assert json.loads(path.read_text())["status"] == "excluded"


def test_live_same_domain_membership_redirect_stops_before_extraction(tmp_path,monkeypatch):
    path=tmp_path / "events/1.json"
    artifact=tmp_path / "artifact.json"
    atomic_json(artifact,{"url":"https://a.test/specials","markdown":"Botox price $10"})
    atomic_json(path,{"event_id":"1","status":"pending","url":"https://a.test/specials","artifact_path":str(artifact)})
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_page",lambda *a, **k:{"accessible":True,"final_url":"https://a.test/membership"})
    monkeypatch.setattr("scripts.run_monitor_price_sync.extract_evidence",lambda *a:pytest.fail("membership must not extract"))
    snapshot={"promotions":[],"services":[],"offers":[],"items":[],"service_names":SERVICE_NAMES,"businesses":[{"business_id":1,"website":"a.test"}]}
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
    monkeypatch.setattr("scripts.run_monitor_price_sync.render_page",lambda *a, **k:{"accessible":True,"final_url":"https://a.test/specials"})
    def fail(*args):
        response=Response();response.status_code=code
        raise HTTPError("do not persist credential-bearing HTTP exception text",response=response)
    monkeypatch.setattr("scripts.run_monitor_price_sync.extract_evidence",fail)
    snapshot={"promotions":[],"services":[],"offers":[],"items":[],"service_names":SERVICE_NAMES,"businesses":[{"business_id":1,"website":"a.test"}]}
    plan=prepare_event(path,snapshot,{},tmp_path)
    assert plan["status"] == "review" and plan["operations"] == []
    assert plan["review"] == [{"reason":reason}]
    assert json.loads(path.read_text())["status"] == status


def test_business_snapshot_filters_services():
    graph=snapshot_business_graph({"promotions":[],"offers":[],"items":[],
        "services":[{"service_id":1,"business_id":1},{"service_id":2,"business_id":2}]},1)
    assert [s["service_id"] for s in graph["services"]] == [1]
