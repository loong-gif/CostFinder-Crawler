from copy import deepcopy

from utils.monitor_price_store import atomic_json
from utils.monitor_price_visual_receipts import reviewed_browser
from utils.monitor_offer_tiers import _visible_key
from utils.monitor_price_extract import extract_evidence

SERVICE_NAMES = ["Botox", "Dermal Filler", "Others"]


def test_reuse_verified_capture_preserves_original_timestamp_and_image_transcription(tmp_path):
    artifact = {"url": "https://clinic.test/specials", "captured_at": "2026-10-10T06:00:00Z", "markdown": "![special](poster.png)"}
    receipt = {"url": artifact["url"], "artifact_captured_at": artifact["captured_at"],
               "artifact_markdown": artifact["markdown"], "observed_at": "2026-10-10T10:00:00Z",
               "job_id": "VR001", "complete": True, "page_status": "accessible",
               "screenshots_viewed": ["poster.png"], "evidence_files": ["poster.png"],
               "verified_text": "Dysport special $12/unit"}
    atomic_json(tmp_path / "visual_verifications.json", [receipt])
    browser = reviewed_browser(tmp_path, artifact, tmp_path / "sync")
    assert browser["text"] == receipt["verified_text"]
    assert browser["prior_visual_receipt"]["observed_at"] == receipt["observed_at"]
    # A new capture at the same URL may have a changed poster behind it.
    later = {**artifact, "captured_at": "2026-10-11T06:00:00Z"}
    assert reviewed_browser(tmp_path, later, tmp_path / "later") is None
    assert reviewed_browser(tmp_path, {**artifact, "markdown": "changed"}, tmp_path / "changed") is None
    for change in [{"complete": False}, {"screenshots_viewed": []}, {"page_status": "not_found"}]:
        atomic_json(tmp_path / "visual_verifications.json", [{**deepcopy(receipt), **change}])
        assert reviewed_browser(tmp_path, artifact, tmp_path / "invalid") is None


def test_prepare_event_refreshes_old_ego_record_for_current_dual_evidence(tmp_path, monkeypatch):
    from scripts import run_monitor_price_sync as runner

    artifact = {"url": "https://clinic.test/specials", "captured_at": "2026-10-10T06:00:00Z", "markdown": "poster"}
    atomic_json(tmp_path / "evidence/a.json", artifact)
    atomic_json(tmp_path / "visual_verifications.json", [{"url": artifact["url"],
                "artifact_captured_at": artifact["captured_at"], "artifact_markdown": "poster",
                "observed_at": "2026-10-10T10:00:00Z", "job_id": "VR001", "complete": True,
                "page_status": "accessible", "screenshots_viewed": ["poster.png"],
                "evidence_files": ["poster.png"], "verified_text": "Dysport $12/unit"}])
    path = tmp_path / "events/1.json"
    atomic_json(path, {"url": artifact["url"], "event_id": "1", "status": "review",
                       "artifact_path": str(tmp_path / "evidence/a.json")})
    snapshot = {"promotions": [], "services": [], "offers": [], "items": [], "terms": [], "service_names": SERVICE_NAMES,
                "businesses": [{"business_id": 1, "website": "clinic.test"}]}
    rendered=[]
    def current_render(url,*args,**kwargs):
        rendered.append(url)
        return {"url":url,"final_url":url,"accessible":True,"text":"current DOM","image_review_required":True}
    monkeypatch.setattr(runner, "render_page", current_render)
    monkeypatch.setattr(runner, "extract_evidence", lambda artifact, browser, directory, client, service_names: {"browser": browser})
    monkeypatch.setattr(runner, "build_plan", lambda source, graph, evidence, protections:
                        {"status": "ready", "operations": [], "review": [], "evidence": evidence})
    plan = runner.prepare_event(path, snapshot, {}, tmp_path)
    assert plan["status"] == "ready", plan
    assert rendered == [artifact["url"]]
    assert "prior_visual_receipt" not in plan["evidence"]["browser"]


def test_chain_page_is_rendered_once_and_planned_for_every_branch(tmp_path, monkeypatch):
    from scripts import run_monitor_price_sync as runner

    url = "https://www.laseraway.com/promotions/"
    atomic_json(tmp_path / "evidence/a.json", {"url": url, "captured_at": "2026-11-07T06:00:00Z", "markdown": "Botox $10"})
    path = tmp_path / "events/1.json"
    atomic_json(path, {"url": url, "event_id": "1", "status": "pending", "artifact_path": str(tmp_path / "evidence/a.json")})
    snapshot = {"promotions": [{"promotion_id": 3807, "business_id": 4385, "source_url": "https://laseraway.com/promotions"},
                               {"promotion_id": 3808, "business_id": 4386, "source_url": "https://laseraway.com/promotions"}],
                "services": [], "offers": [], "items": [], "terms": [], "service_names": SERVICE_NAMES,
                "businesses": [{"business_id": 4385, "website": "laseraway.com", "address": "8500 Melrose Ave, West Hollywood, CA"},
                               {"business_id": 4386, "website": "laseraway.com", "address": "1 Grand Ave, Austin, TX"}]}
    rendered, extracted, planned = [], [], []

    def render(page_url, *args, **kwargs):
        rendered.append(page_url)
        return {"url": page_url, "final_url": page_url, "accessible": True, "text": "Botox $10", "image_review_required": False}

    def plan(source, graph, evidence, protections):
        planned.append((source["business_id"], [p["promotion_id"] for p in graph["promotions"]]))
        return {"status": "ready", "source": source, "operations": [{"table": "clinic_promotions", "action": "update",
                "id": graph["promotions"][0]["promotion_id"], "values": {}}], "review": []}

    monkeypatch.setattr(runner, "render_page", render)
    monkeypatch.setattr(runner, "extract_evidence", lambda *a: extracted.append(1) or {"offers": []})
    monkeypatch.setattr(runner, "build_plan", plan)
    result = runner.prepare_event(path, snapshot, {}, tmp_path)
    assert rendered == [url] and extracted == [1]
    assert planned == [(4385, [3807]), (4386, [3808])]
    assert result["status"] == "ready" and result["source"] == {"url": url, "business_ids": [4385, 4386]}
    assert [b["source"]["business_id"] for b in result["branches"]] == [4385, 4386]
    assert [op["id"] for op in result["operations"]] == [3807, 3808]
    assert all(b["event_id"] == "1" and b["evidence_path"] == result["evidence_path"] for b in result["branches"])


def test_visual_reuse_uses_verified_text_and_ignores_layout_punctuation(tmp_path):
    quote = "Enjoy 25% off SkinMedica products in-spa, while supplies last. *excludes TNS serum."
    copied = "enjoy 25% off skinmedica products in-spa, while supplies last *excludes tns serum"
    assert _visible_key(quote) == _visible_key(copied)
    assert _visible_key("Botox $10.99/unit") != _visible_key("Botox $10/unit")
    assert _visible_key("Botox 10% off") != _visible_key("Botox $10 off")
    class Client:
        def create_json_response(self, messages, **kwargs):
            assert "STALE $9/unit" not in messages[1]["content"]
            assert kwargs["json_schema"]["properties"]["offers"]["items"]["properties"]["offer_raw_text"]["enum"] == [quote]
            return {"offers": [{"offer_raw_text": quote, "items": []}], "all_offers_extracted": True}
    browser = {"accessible": True, "complete": True, "text": quote,
               "image_review_required": False, "verified_quotes": [quote],
               "prior_visual_receipt": {"observed_at": "2026-10-10T10:00:00Z"}}
    evidence = extract_evidence({"url": "https://clinic.test", "markdown": "STALE $9/unit"}, browser, tmp_path, Client(), SERVICE_NAMES)
    assert evidence["offers"][0]["visibility"] == "unverified"
    assert evidence["image_policy"]["hold_parent_fields"] is True
    assert not evidence["complete"]
    assert evidence["prior_visual_receipt"]["observed_at"] == "2026-10-10T10:00:00Z"
