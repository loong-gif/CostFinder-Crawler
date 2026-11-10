from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from monitor_image_support import proof_plan
from monitor_price_plan_support import RAW
from utils.monitor_image_gate import image_write_block
from utils.monitor_price_store import atomic_json, now

DEACTIVATE = {"table": "promo_offer_master", "action": "update", "id": 4, "values": {"is_active": False}}


def text_plan(tmp_path, offers, browser=None, operations=()):
    """没有图片策略的计划：页面文字证据与可见性证据都在计划目录里。"""
    atomic_json(tmp_path / "evidence.json", {"offers": offers})
    atomic_json(tmp_path / "browser.json", browser if browser is not None else {"url": "https://test", "text": RAW})
    return {"evidence_path": str(tmp_path / "evidence.json"), "operations": list(operations)}


def variant_offer(tmp_path, **capture):
    shot = tmp_path / "variant.png"
    shot.write_bytes(b"png")
    return {"offer_raw_text": RAW, "visibility": "visible_match",
            "variant_evidence": {"screenshot_path": str(shot), "captured_at": now(), "offer_raw_text": RAW, **capture}}


def test_current_variant_capture_of_the_same_text_passes(tmp_path):
    assert image_write_block(text_plan(tmp_path, [variant_offer(tmp_path)]), tmp_path / "journals") is None


@pytest.mark.parametrize("capture", [
    {"screenshot_path": "missing.png"},
    {"captured_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()},
    {"offer_raw_text": "Botox 50 units Special $300"},
])
def test_variant_capture_must_be_saved_current_and_of_the_same_text(tmp_path, capture):
    plan = text_plan(tmp_path, [variant_offer(tmp_path, **capture)])
    assert image_write_block(plan, tmp_path / "journals") == "variant_capture_proof_mismatch"


@pytest.mark.parametrize("browser, offers", [
    ({}, []),
    ({"url": "https://test", "image_review_required": True}, []),
    ({"url": "https://test"}, [{"offer_raw_text": RAW, "visibility": "image_only"}]),
])
def test_plan_without_image_policy_cannot_rely_on_image_evidence(tmp_path, browser, offers):
    assert image_write_block(text_plan(tmp_path, offers, browser), tmp_path / "journals") == \
        "legacy_image_evidence_requires_refresh"


def test_crawler_evidence_supports_a_removal_without_screenshots(tmp_path):
    crawler = {"visibility_source": "crawler_html_filtered", "url": "https://test", "accessible": True,
               "complete": True, "image_review_required": False, "text": RAW}
    plan = text_plan(tmp_path, [{"offer_raw_text": RAW, "visibility": "visible_match"}], crawler, [DEACTIVATE])
    assert image_write_block(plan, tmp_path / "journals") is None
    atomic_json(tmp_path / "browser.json", {**crawler, "visibility_source": "browser_render"})
    assert image_write_block(plan, tmp_path / "journals") == "breakpoint_coverage_incomplete"


@pytest.mark.parametrize("operation, block", [
    (DEACTIVATE, "breakpoint_coverage_incomplete"),
    ({**DEACTIVATE, "values": {"is_active": True}}, None),
    ({"table": "clinic_promotions", "action": "update", "id": 3, "values": {"promotion_title": "Specials"}},
     "breakpoint_coverage_incomplete"),
    ({"table": "clinic_promotions", "action": "update", "id": 3, "values": {"is_offer_extracted": True}}, None),
    ({"table": "clinic_promotions", "action": "insert", "id": None, "values": {"promotion_title": "Specials"}}, None),
    ({"table": "promo_offer_items", "action": "update", "id": 5, "values": {"is_active": False}}, None),
])
def test_only_changes_to_existing_rows_need_complete_viewport_coverage(tmp_path, operation, block):
    browser = {"visibility_source": "browser_render", "url": "https://test", "text": RAW,
               "breakpoint_coverage": {"desktop": {"complete": False}}}
    plan = text_plan(tmp_path, [{"offer_raw_text": RAW, "visibility": "visible_match"}], browser, [operation])
    assert image_write_block(plan, tmp_path / "journals") == block


@pytest.mark.parametrize("field", ["url", "visual_digest", "context_digest"])
def test_dual_read_proof_must_come_from_this_capture(tmp_path, field):
    plan, proof = proof_plan(tmp_path)
    atomic_json(tmp_path / "dual.json", {**proof, field: "another capture"})
    assert image_write_block(plan, tmp_path / "journals") == "image_capture_proof_mismatch"


INSERT_PARENT = {"table": "clinic_promotions", "action": "insert", "id": None, "values": {}}


@pytest.mark.parametrize("offer, operation, block", [
    ({"offer_raw_text": RAW, "visibility": "visible_match"}, INSERT_PARENT, None),
    ({"offer_raw_text": "Filler $500", "visibility": "visible_match"}, INSERT_PARENT, "image_parent_facts_missing"),
    ({"offer_raw_text": RAW, "visibility": "unverified"}, INSERT_PARENT, "image_parent_facts_missing"),
    ({"offer_raw_text": "Filler $500", "visibility": "visible_match",
      "variant_evidence": {"offer_raw_text": "Filler $500"}}, INSERT_PARENT, None),
    ({"offer_raw_text": "Filler $500", "visibility": "visible_match"},
     {**INSERT_PARENT, "action": "update", "values": {"is_offer_extracted": True}}, None),
    ({"offer_raw_text": "Filler $500", "visibility": "visible_match"},
     {**INSERT_PARENT, "table": "promo_offer_master"}, None),
])
def test_new_parent_needs_image_facts_or_an_offer_grounded_in_the_page_text(tmp_path, offer, operation, block):
    plan, _ = proof_plan(tmp_path)
    browser = {**read(tmp_path / "browser.json"), "text": RAW}
    atomic_json(tmp_path / "browser.json", browser)
    atomic_json(tmp_path / "dual.json", {**read(tmp_path / "dual.json"), "text": RAW})
    if "variant_evidence" in offer:
        offer = {**offer, "variant_evidence": variant_offer(tmp_path, **offer["variant_evidence"])["variant_evidence"]}
    atomic_json(tmp_path / "evidence.json", {"offers": [offer]})
    plan["operations"] = [operation]
    plan["image_policy"]["candidate_facts"] = []
    assert image_write_block(plan, tmp_path / "journals") == block


def read(path):
    import json
    return json.loads(path.read_text())
