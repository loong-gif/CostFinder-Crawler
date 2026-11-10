"""Visibility of crawler HTML computed from the page's own CSS, used to decide whether a browser render is needed.

The cascade of the hiding-relevant properties is resolved from the raw HTML and the fetched stylesheets for a
1440px-wide screen at rest. ``assess_page`` returns ``needs_browser=True`` whenever a stylesheet, selector,
condition or JavaScript-driven content leaves visibility undecided, so the browser stays the judge for those pages.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from bs4 import NavigableString

from utils.crawler_visibility_cascade import Cascade
from utils.crawler_visibility_rules import (
    CONDITION_UNDECIDABLE, SELECTOR_UNDECIDABLE, STYLESHEET_UNAVAILABLE, RuleCollector,
)
from utils.crawler_visibility_sheets import parse_document
from utils.crawler_visibility_values import VALUE_UNDECIDABLE, Box, compute_boxes

DESKTOP_WIDTH = 1440
PROMO_IMAGE_HINT = re.compile(r"special|promo|offer|sale|deal|discount|off\b|%|\$\d|save|membership|package|"
                              r"banner|coupon|limited", re.I)
# 轮播组件在加载时由 JavaScript 重排、克隆和隐藏幻灯片，层叠计算回答不了它们的静止状态
JS_WIDGET_CLASS = re.compile(r"(?:^|[-_])(?:swiper|slick|carousel|splide|flickity|glide|owl)(?:$|[-_])", re.I)
# 滚动动画库在加载后由 JavaScript 先隐藏、进入视口时再显示内容
ANIMATION_ATTRIBUTES = frozenset({"data-aos", "data-animation", "data-animate", "data-sal", "data-wow-delay"})
ANIMATION_CLASSES = frozenset({"wow", "animate__animated"})
EMBEDDED_TAGS = ("iframe", "object", "embed", "canvas")
CSS_UNCERTAIN_REASONS = frozenset({STYLESHEET_UNAVAILABLE, SELECTOR_UNDECIDABLE, CONDITION_UNDECIDABLE,
                                   VALUE_UNDECIDABLE})
MIN_VISIBLE_CHARS = 200


@dataclass(frozen=True)
class PageAssessment:
    needs_browser: bool
    reasons: Tuple[str, ...]
    visible_text: str
    visible_digest: str


def _visible_text(soup, boxes: Dict[int, Box]) -> str:
    chunks: List[str] = []
    for node in soup.descendants:
        if type(node) is not NavigableString or not node.strip():
            continue
        box = boxes.get(id(node.parent))
        if box and not (box.hidden or box.text_hidden or box.textless or box.details_closed):
            chunks.append(str(node))
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()


def _markup_reasons(elements: List, boxes: Dict[int, Box]) -> Set[str]:
    reasons: Set[str] = set()
    for element in elements:
        box = boxes[id(element)]
        if box.hidden:
            continue
        if element.name in ("img", "source"):
            hint = " ".join(str(element.get(k, "")) for k in ("alt", "src", "srcset", "title"))
            if PROMO_IMAGE_HINT.search(hint):
                reasons.add("image_promo_candidate")
        elif element.name in EMBEDDED_TAGS:
            reasons.add("embedded_content")
        classes = element.get("class", [])
        if any(JS_WIDGET_CLASS.search(name) for name in classes):
            reasons.add("js_widget_state")
        animated = any(str(element.get(name, "none")).strip().lower() not in ("none", "") for name in ANIMATION_ATTRIBUTES)
        if animated or ANIMATION_CLASSES & set(classes):
            reasons.add("js_animation")
    return reasons


def _numeric_tokens(fact: str) -> List[str]:
    return re.findall(r"\d+(?:[.,]\d+)*", str(fact))


def assess_page(markdown: str, raw_html: Optional[str], facts: Iterable[str] = (), *, page_url: str,
                stylesheets: Mapping[str, Optional[str]], width: float = DESKTOP_WIDTH) -> PageAssessment:
    """Decide whether crawler evidence is enough or the page must go to the browser.

    ``stylesheets`` maps absolute stylesheet URLs to their text (``None`` when unavailable), as returned by
    ``utils.crawler_visibility_fetch.fetch_stylesheets``.
    """
    reasons: Set[str] = set()
    if not (markdown or "").strip():
        reasons.add("empty_markdown")
    if not (raw_html or "").strip():
        reasons.add("no_html")
        return PageAssessment(True, tuple(sorted(reasons)), "", "")
    soup = parse_document(raw_html)
    collector = RuleCollector(stylesheets, width)
    cascade = Cascade(soup, collector.collect(soup, page_url))
    boxes, value_reasons = compute_boxes(cascade)
    text = _visible_text(soup, boxes)
    reasons |= (collector.reasons | cascade.reasons | value_reasons | _markup_reasons(cascade.index.elements, boxes)
                | _text_reasons(text, len(soup.find_all("script")), facts))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
    return PageAssessment(bool(reasons), tuple(sorted(reasons)), text, digest)


def _text_reasons(text: str, scripts: int, facts: Iterable[str]) -> Set[str]:
    reasons: Set[str] = set()
    if len(text) < MIN_VISIBLE_CHARS:
        reasons.add("thin_visible_text")
    if scripts >= 8 and len(text) < 2000:
        reasons.add("script_heavy_page")
    lowered = text.casefold()
    if any((tokens := _numeric_tokens(fact)) and not all(token in lowered for token in tokens) for fact in facts):
        reasons.add("claim_not_in_visible_text")
    return reasons
