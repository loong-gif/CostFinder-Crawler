"""Hidden-element filtering of crawler HTML, used to decide whether a browser render is needed.

This is DOM/inline-style evidence, not a rendered layout: it cannot see CSS classes or JavaScript.
``assess_page`` therefore returns ``needs_browser=True`` whenever the page might hide or inject
content in ways this filter cannot judge, so the browser stays the fallback for those pages.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable, List, Sequence, Tuple

VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                       "source", "track", "wbr"})
ALWAYS_HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript", "head", "title", "svg", "iframe",
                                "object", "canvas", "select", "option"})
HIDING_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*(hidden|collapse)|opacity\s*:\s*0(\.0+)?\s*(;|$)|"
    r"font-size\s*:\s*0(px|em|rem|pt|%)?\s*(;|$)|"
    r"(text-indent|left|right|top)\s*:\s*-\d{3,}px|clip\s*:\s*rect\(\s*0|clip-path\s*:\s*inset\(\s*50%|"
    r"(?:^|;)\s*(max-)?(width|height)\s*:\s*0(px)?\s*(;|$)",
    re.I,
)
SR_ONLY_CLASSES = frozenset({"sr-only", "screen-reader-text", "visually-hidden", "visuallyhidden", "a11y-hidden",
                             "assistive-text", "screen-reader-only"})
CSS_CONTROLLED_CLASSES = frozenset({"hidden", "hide", "d-none", "invisible", "collapse", "collapsed", "is-hidden",
                                    "display-none", "u-hidden", "tw-hidden", "mobile-hidden", "desktop-hidden",
                                    "hidden-xs", "hidden-sm", "hidden-md", "hidden-lg", "show-more", "read-more",
                                    "accordion-content", "tab-pane", "modal", "popup", "dropdown-menu"})
PROMO_IMAGE_HINT = re.compile(r"special|promo|offer|sale|deal|discount|off\b|%|\$\d|save|membership|package|"
                              r"banner|coupon|limited", re.I)
MIN_VISIBLE_CHARS = 200


@dataclass(frozen=True)
class PageAssessment:
    needs_browser: bool
    reasons: Tuple[str, ...]
    visible_text: str
    visible_digest: str


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[Tuple[str, bool]] = []
        self.chunks: List[str] = []
        self.reasons: set[str] = set()
        self.script_count = 0
        self.closed_details: List[bool] = []

    @property
    def hidden(self) -> bool:
        return any(flag for _, flag in self.stack)

    def _is_hidden(self, tag: str, attrs: dict[str, str]) -> Tuple[bool, bool]:
        """Return (hidden, css_uncertain)."""
        if tag in ALWAYS_HIDDEN_TAGS:
            return True, False
        if "hidden" in attrs or attrs.get("aria-hidden", "").strip().lower() == "true":
            return True, False
        if tag == "input" and attrs.get("type", "").lower() == "hidden":
            return True, False
        if tag == "dialog" and "open" not in attrs:
            return True, False
        parent = self.stack[-1][0] if self.stack else ""
        if parent == "details" and tag != "summary" and self.closed_details and self.closed_details[-1]:
            return True, False  # body of a collapsed <details>; only its <summary> stays visible
        if HIDING_STYLE.search(attrs.get("style", "")):
            return True, False
        classes = {c.lower() for c in attrs.get("class", "").split()}
        if classes & SR_ONLY_CLASSES:
            return True, False
        return False, bool(classes & CSS_CONTROLLED_CLASSES)

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        if tag == "script":
            self.script_count += 1
        if tag in ("img", "source"):
            hint = " ".join(attrs.get(k, "") for k in ("alt", "src", "srcset", "title"))
            if not self.hidden and PROMO_IMAGE_HINT.search(hint):
                self.reasons.add("image_promo_candidate")
        if tag in VOID_TAGS:
            return
        hidden, uncertain = self._is_hidden(tag, attrs)
        if tag == "summary" and any(t == "details" for t, _ in self.stack):
            hidden = False  # the summary stays visible inside a closed <details>
        if tag == "details":
            self.closed_details.append("open" not in attrs)
        self.stack.append((tag, hidden))
        if uncertain and not self.hidden:
            self.reasons.add("css_class_hiding_possible")

    def handle_endtag(self, tag: str) -> None:
        if tag == "details" and self.closed_details:
            self.closed_details.pop()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.chunks.append(data)


def visible_text_from_html(html: str) -> Tuple[str, set[str], int]:
    """Return (visible text, uncertainty reasons, script count) for crawler HTML."""
    parser = _VisibleTextParser()
    parser.feed(html or "")
    parser.close()
    text = re.sub(r"\s+", " ", " ".join(parser.chunks)).strip()
    return text, parser.reasons, parser.script_count


def _numeric_tokens(fact: str) -> List[str]:
    return re.findall(r"\d+(?:[.,]\d+)*", str(fact))


def assess_page(markdown: str, raw_html: str | None, facts: Iterable[str] = ()) -> PageAssessment:
    """Decide whether crawler evidence is enough or the page must go to the browser."""
    reasons: set[str] = set()
    if not (markdown or "").strip():
        reasons.add("empty_markdown")
    if not (raw_html or "").strip():
        reasons.add("no_html")
        return PageAssessment(True, tuple(sorted(reasons)), "", "")
    text, uncertain, scripts = visible_text_from_html(raw_html)
    reasons |= uncertain
    if len(text) < MIN_VISIBLE_CHARS:
        reasons.add("thin_visible_text")
    if scripts >= 8 and len(text) < 2000:
        reasons.add("script_heavy_page")
    lowered = text.casefold()
    for fact in facts:
        tokens = _numeric_tokens(fact)
        if tokens and not all(token in lowered for token in tokens):
            reasons.add("claim_not_in_visible_text")
            break
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
    return PageAssessment(bool(reasons), tuple(sorted(reasons)), text, digest)
