"""Write-time gate for promotion evidence verified in a rendered browser page."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from utils.render_review import corpus_text


@dataclass(frozen=True)
class RenderedVisibilityDecision:
    accepted: bool
    content: tuple[str, ...]
    removed: tuple[str, ...]
    reason: str


def normalize_claim(value: Any) -> str:
    text = str(value or "").strip()
    return " ".join(text.replace("\u2013", "-").replace("\u2014", "-").split()).casefold()



IMAGE_OCR_PROOF = "rendered_image_ocr"
SCREENSHOT_PROOFS = frozenset({"rendered_screenshot_ocr", "ego_verified_capture"})


def image_claim_verified(claim: Any, item: Mapping[str, Any], page: Mapping[str, Any]) -> bool:
    """An image label is not evidence: bind the readable quotation to its capture.

    ``rendered_image_ocr`` must name the visible images whose recorded OCR text forms the transcript;
    ``rendered_screenshot_ocr`` / ``ego_verified_capture`` bind the transcript to an existing screenshot.
    """
    proof = item.get("visibleEvidence")
    if not isinstance(proof, Mapping) or proof.get("kind") not in {IMAGE_OCR_PROOF, *SCREENSHOT_PROOFS}:
        return False
    text = normalize_claim(claim)
    transcript = normalize_claim(proof.get("transcript"))
    if not text or text not in transcript or len(re.findall(r"[^\W\d_]{2,}", text)) < 2:
        return False
    if not proof.get("pageUrl") or proof.get("pageUrl") != page.get("url"):
        return False
    capture_time = page.get("checked_at") or page.get("captured_at")
    if not proof.get("captured_at") or proof.get("captured_at") != capture_time:
        return False
    if proof["kind"] == IMAGE_OCR_PROOF:
        urls = proof.get("imageUrls") or []
        visible = set(page.get("visibleImages") or [])
        readable = {image.get("url"): image.get("text") for image in page.get("imageOcr") or []
                    if isinstance(image, Mapping) and image.get("text")}
        evidence = proof.get("ocrEvidence") or []
        if any(not isinstance(image, Mapping) for image in evidence):
            return False
        if (not urls or not set(urls).issubset(visible) or set(urls) != {image.get("url") for image in evidence}
                or any(readable.get(image.get("url")) != image.get("text") or not image.get("text")
                       for image in evidence)
                or normalize_claim(corpus_text([image["text"] for image in evidence])) != transcript):
            return False
        numbers = re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?:\s*%)?", text)
        return all(number in transcript for number in numbers)
    screenshot = proof.get("screenshotPath")
    if not isinstance(screenshot, str) or not Path(screenshot).is_file():
        return False
    # Preserve decimal/currency/percent characters; substring token overlap is insufficient.
    numbers = re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?:\s*%)?", text)
    return all(number in transcript for number in numbers)


def current_page_manifest(browser: Mapping[str, Any], record_type: str, claims: Sequence[str],
                          *, image_transcript: str = "", image_complete: bool = False) -> dict:
    """Build the shared claim receipt from the current visible union and image read."""
    report = {"status":"page_accessible" if browser.get("accessible") else "page_not_accessible",
              "url":browser.get("url"),"captured_at":browser.get("captured_at"),
              "breakpoint_coverage":browser.get("breakpoint_coverage"),
              "complete":bool(browser.get("complete") and (not browser.get("image_review_required") or image_complete)),
              "claimResults":[]}
    visible = normalize_claim(browser.get("text"))
    for claim in claims:
        value = normalize_claim(claim)
        item = {"recordType":record_type,"claim":claim,"status":"not_found"}
        if report["status"] == "page_accessible" and value and value in visible:
            item["status"] = "visible_match"
        elif report["status"] == "page_accessible" and image_complete:
            item["visibleEvidence"] = {"kind":"rendered_screenshot_ocr","pageUrl":browser.get("url"),
                "captured_at":browser.get("captured_at"),"screenshotPath":browser.get("screenshot_path"),
                "transcript":image_transcript}
            if image_claim_verified(claim, item, report):
                item["status"] = "image_only"
        report["claimResults"].append(item)
    return report

def filter_visible_source_content(
    content: Sequence[str],
    rendered_visibility: Mapping[str, Any] | None,
    *,
    record_type: str,
) -> RenderedVisibilityDecision:
    """Keep source claims proven visible by the browser gate.

    ``rendered_visibility`` is one page result from the ego visibility report:
    ``{"status": "page_accessible", "claimResults": [...]}``.
    Firecrawl Markdown, HTML presence, or a missing result is deliberately
    insufficient.
    """
    values = tuple(str(value).strip() for value in content if str(value).strip())
    if not isinstance(rendered_visibility, Mapping):
        return RenderedVisibilityDecision(False, (), values, "rendered_visibility_required")
    if rendered_visibility.get("status") != "page_accessible":
        return RenderedVisibilityDecision(False, (), values, "page_not_accessible")

    visible = {
        normalize_claim(item.get("claim"))
        for item in rendered_visibility.get("claimResults") or []
        if isinstance(item, Mapping)
        and item.get("recordType") == record_type
        and (item.get("status") == "visible_match" or
             (item.get("status") == "image_only" and image_claim_verified(item.get("claim"), item, rendered_visibility)))
        and normalize_claim(item.get("claim"))
    }
    kept = tuple(value for value in values if normalize_claim(value) in visible)
    removed = tuple(value for value in values if normalize_claim(value) not in visible)
    if not kept:
        return RenderedVisibilityDecision(
            False,
            (),
            removed,
            f"no_rendered_visible_{record_type}_content",
        )
    return RenderedVisibilityDecision(True, kept, removed, "visible_content_filtered")


def filter_visible_promotion_content(
    content: Sequence[str],
    rendered_visibility: Mapping[str, Any] | None,
) -> RenderedVisibilityDecision:
    """Keep only Promotion claims proven visible by the browser gate."""
    return filter_visible_source_content(
        content,
        rendered_visibility,
        record_type="promotion",
    )


def filter_visible_membership_benefits(
    benefits: Sequence[str],
    rendered_visibility: Mapping[str, Any] | None,
) -> RenderedVisibilityDecision:
    """Keep only Membership benefit claims proven visible by the browser gate."""
    return filter_visible_source_content(
        benefits,
        rendered_visibility,
        record_type="membership",
    )
