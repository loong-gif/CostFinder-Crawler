from __future__ import annotations

import re
from pathlib import Path

PROMO_PRICE_TEXT = re.compile(r"\$\s*\d|\d\s*%\s*off|\b(?:\w+\s+off|save\s+\$?\d+|discount\w*|bogo)\b", re.I)
PROMO_ALT = re.compile(r"\b(?:promo\w*|special\w*|offers?|sale|deals?|discount\w*|save|savings|off|free|bogo|packages?|bundles?|"
                       r"black\s*friday|cyber\s*monday|holiday|christmas|valentine\w*|mother'?s\s*day|"
                       r"new\s*year|limited[- ]time|event)\b|\$\s*\d|\d\s*%", re.I)


def text_has_promotion(text: str) -> bool:
    return bool(PROMO_PRICE_TEXT.search(text or ""))


def promotion_images(frames: list[dict]) -> list[dict]:
    """Images whose alt text suggests a promotion; they are checked only when the page text has none."""
    return [image for frame in frames for image in frame.get("images", []) if PROMO_ALT.search(image.get("alt") or "")]


def resources_complete(frames: list[dict], text: str = "") -> bool:
    if not all(frame.get("ready") for frame in frames):
        return False
    if text_has_promotion(text):
        return True
    return (not any(frame.get("unreviewed_regions") for frame in frames)
            and all(image.get("loaded") is True for image in promotion_images(frames)))


def confirmed_not_found(browser: dict) -> bool:
    return bool(browser.get("http_status") == 404 and browser.get("scroll_complete")
                and Path(browser.get("screenshot_path", "")).is_file()
                and not browser.get("failures") and re.search(
                    r"\bpage\b.{0,30}\b(?:not\s+found|does not exist|doesn't exist)\b|"
                    r"\b404\s*[-: ]*\s*(?:page\s*)?(?:not\s+found|error)\b",
                    browser.get("text", ""), re.I))
