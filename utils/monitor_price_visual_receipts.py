"""Reuse persisted human browser verification for its original capture only."""
from __future__ import annotations

from pathlib import Path

from utils.monitor_price_store import atomic_json, read_json
from utils.monitor_price_writer_plan import normalize_source_url
from utils.monitor_visibility_evidence import CRAWLER_SOURCE, visibility_source


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


def captured_visibility(artifact: dict) -> dict | None:
    """抓取阶段保存的可见性证据：渲染结果文件，或存在抓取证据里的爬虫证据。"""
    rendered = artifact.get("rendered", {})
    if rendered.get("browser_path"):
        return read_json(Path(rendered["browser_path"]), {}) or None
    if visibility_source(rendered.get("browser", {})) == CRAWLER_SOURCE:
        return rendered["browser"]
    return None


def stored_browser(root: Path, artifact: dict, artifact_path: str, directory: Path) -> dict | None:
    """抓取时保存的可见性证据；没有时复用对同一次抓取做过的 Ego Lite 核验（截图仍在才算），保留原始时间。"""
    browser = captured_visibility(artifact)
    if browser:
        return browser
    verified = reviewed_browser(root, artifact, directory)
    if not verified:
        return None
    receipt = next(r for r in read_json(root / "visual_verifications.json", [])
                   if r.get("url") == artifact.get("url") and r.get("artifact_captured_at") == artifact.get("captured_at")
                   and r.get("artifact_markdown") == artifact.get("markdown"))
    if not all(Path(f).is_file() for f in receipt.get("screenshots_viewed", [])):
        return None
    coverage = receipt.get("breakpoint_coverage")
    return {**verified, "captured_at": artifact["captured_at"], "verified_capture": True,
            "url": artifact["url"], "visual_receipt_path": str(root / "visual_verifications.json"),
            "verified_artifact_path": artifact_path, "breakpoint_coverage": coverage,
            "complete": bool(coverage and all(v.get("complete") for v in coverage.values()))}
