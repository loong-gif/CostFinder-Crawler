from __future__ import annotations

import pytest

from utils.monitor_price_store import atomic_json
from utils.monitor_price_visual_receipts import stored_browser

ARTIFACT = {"url": "https://clinic.test/specials", "captured_at": "2026-10-10T06:00:00Z", "markdown": "poster"}


def receipt(tmp_path, **changes):
    shot = tmp_path / "poster.png"
    shot.write_bytes(b"png")
    return {"url": ARTIFACT["url"], "artifact_captured_at": ARTIFACT["captured_at"], "artifact_markdown": "poster",
            "observed_at": "2026-10-10T10:00:00Z", "job_id": "VR001", "complete": True, "page_status": "accessible",
            "screenshots_viewed": [str(shot)], "evidence_files": [str(shot)], "verified_text": "Dysport $12/unit",
            "breakpoint_coverage": {"desktop": {"complete": True}}, **changes}


def test_saved_render_of_the_capture_is_the_evidence(tmp_path):
    atomic_json(tmp_path / "rendered/browser.json", {"url": ARTIFACT["url"], "accessible": True, "text": "DOM"})
    artifact = {**ARTIFACT, "rendered": {"browser_path": str(tmp_path / "rendered/browser.json")}}
    assert stored_browser(tmp_path, artifact, "a.json", tmp_path / "sync")["text"] == "DOM"


def test_crawler_evidence_saved_with_the_capture_is_the_evidence(tmp_path):
    crawler = {"visibility_source": "crawler_html_filtered", "accessible": True, "complete": True,
               "image_review_required": False, "text": "Botox $10/unit"}
    assert stored_browser(tmp_path, {**ARTIFACT, "rendered": {"browser": crawler}}, "a.json", tmp_path / "sync") == crawler


def test_capture_without_evidence_has_none(tmp_path):
    assert stored_browser(tmp_path, {**ARTIFACT, "rendered": {}}, "a.json", tmp_path / "sync") is None


@pytest.mark.parametrize("other", [
    {"url": "https://other.test/specials"},
    {"artifact_captured_at": "2026-10-09T06:00:00Z"},
])
def test_human_verification_is_taken_from_the_receipt_of_this_capture(tmp_path, other):
    atomic_json(tmp_path / "visual_verifications.json",
                [receipt(tmp_path, screenshots_viewed=[str(tmp_path / "gone.png")], **other), receipt(tmp_path)])
    browser = stored_browser(tmp_path, ARTIFACT, "a.json", tmp_path / "sync")
    assert browser["verified_capture"] is True and browser["complete"] is True


def test_human_verification_without_viewport_coverage_is_incomplete(tmp_path):
    atomic_json(tmp_path / "visual_verifications.json", [receipt(tmp_path, breakpoint_coverage=None)])
    assert stored_browser(tmp_path, ARTIFACT, "a.json", tmp_path / "sync")["complete"] is False
