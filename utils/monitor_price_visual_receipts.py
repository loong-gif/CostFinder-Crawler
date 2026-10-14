"""Reuse persisted human browser verification for its original capture only."""
from __future__ import annotations

from pathlib import Path

from utils.monitor_price_store import atomic_json, read_json
from utils.monitor_price_writer import normalize_source_url


def reviewed_browser(root: Path, artifact: dict, directory: Path) -> dict | None:
    for receipt in read_json(root / "visual_verifications.json", []):
        if normalize_source_url(receipt["url"]) != normalize_source_url(artifact["url"]):
            continue
        # A later scrape can contain changed image bytes behind the same URL.
        # Unchanged markdown alone must never re-authorize an old image price.
        if (not receipt.get("artifact_captured_at") or not receipt.get("verified_text")
                or receipt.get("artifact_captured_at") != artifact.get("captured_at")
                or receipt.get("artifact_markdown") != artifact.get("markdown")
                or receipt.get("page_status") != "accessible"
                or not receipt.get("complete") or not receipt.get("screenshots_viewed")):
            continue
        browser = {"accessible": True, "complete": True,
                   "final_url": receipt.get("final_url") or receipt["url"],
                   "text": receipt["verified_text"], "image_review_required": False,
                   "verified_quotes": [o["quote"] for o in receipt.get("visible_offers", []) if o.get("quote")],
                   "prior_visual_receipt": {"observed_at": receipt["observed_at"],
                                            "job_id": receipt["job_id"],
                                            "evidence_files": receipt["evidence_files"]}}
        atomic_json(directory / "browser.json", browser)
        return browser
    return None
